import asyncio
import json
import socket
from contextlib import asynccontextmanager

import httpx
import pytest
import pytest_asyncio
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import AnyUrl

from tests.protocol_client import CursorStore, consume, http_client_factory
from hotel_mcp.server import create_app
from hotel_mcp.store import HotelStore, Principal


@pytest_asyncio.fixture
async def hotel(tmp_path):
    principals = [
        {"id": "room", "hotel": "hotel-a", "role": "room", "room": "101", "token": "r" * 40},
        {"id": "desk", "hotel": "hotel-a", "role": "frontdesk", "token": "d" * 40},
        {"id": "neighbor", "hotel": "hotel-a", "role": "room", "room": "102", "token": "n" * 40},
        {"id": "other", "hotel": "hotel-b", "role": "frontdesk", "token": "o" * 40},
    ]
    app = create_app({"principals": principals}, tmp_path / "hotel.db")
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(.02)
    assert server.started
    configs = {row["id"]: {"url": f"http://127.0.0.1:{port}/{row['id']}/mcp",
                           "headers": {"Authorization": "Bearer " + row["token"]}} for row in principals}
    try:
        yield configs
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)
        sock.close()


@asynccontextmanager
async def client(config):
    async with streamablehttp_client(**config, httpx_client_factory=http_client_factory) as (read, write, session_id):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session, session_id


def result(value):
    assert not value.isError, value
    return json.loads(value.content[0].text)


@pytest.mark.asyncio
async def test_real_mcp_round_trip_and_reconnect(hotel, tmp_path):
    cursors = CursorStore(tmp_path / "cursors.db")
    desk_events, room_events = asyncio.Queue(), asyncio.Queue()
    desk = asyncio.create_task(consume(hotel["desk"], cursors, "desk", desk_events.put, poll_seconds=60))
    room = asyncio.create_task(consume(hotel["room"], cursors, "room", room_events.put, poll_seconds=60))
    try:
        # Allow initial empty read to finish, so receipt below requires a real MCP notification.
        await asyncio.sleep(.4)
        async with client(hotel["room"]) as (room_session, _), client(hotel["desk"]) as (desk_session, _):
            submitted = result(await room_session.call_tool("hotel_submit_request", {"request_key": "turn-water-1", "description": "请送两瓶水"}))
            event = await asyncio.wait_for(desk_events.get(), 3)
            assert "101" in event["speech_text"] and "两瓶水" in event["speech_text"]
            again = result(await room_session.call_tool("hotel_submit_request", {"request_key": "turn-water-1", "description": "请送两瓶水"}))
            assert again["id"] == submitted["id"]
            accepted = result(await desk_session.call_tool("hotel_accept_request", {"request_id": submitted["id"], "note": "五分钟内送到"}))
            assert accepted["status"] == "accepted"
            feedback = await asyncio.wait_for(room_events.get(), 3)
            assert "受理" in feedback["speech_text"] and "五分钟" in feedback["speech_text"]
            room.cancel()
            await asyncio.gather(room, return_exceptions=True)
            result(await desk_session.call_tool("hotel_complete_request", {"request_id": submitted["id"]}))
            # Recreate cursor store as a process restart would; missed event is replayed once.
            room = asyncio.create_task(consume(hotel["room"], CursorStore(tmp_path / "cursors.db"), "room", room_events.put, poll_seconds=60))
            completed = await asyncio.wait_for(room_events.get(), 3)
            assert "已完成" in completed["speech_text"]
            assert feedback["event_id"] != completed["event_id"]
            await asyncio.sleep(.15)
            assert desk_events.empty() and room_events.empty()
    finally:
        desk.cancel(); room.cancel()
        await asyncio.gather(desk, room, return_exceptions=True)


@pytest.mark.asyncio
async def test_role_room_hotel_and_session_isolation(hotel):
    async with client(hotel["room"]) as (room, session_id):
        order = result(await room.call_tool("hotel_submit_request", {"request_key": "one", "description": "送水"}))
        assert (await room.call_tool("hotel_accept_request", {"request_id": order["id"]})).isError
        assert (await room.call_tool("hotel_submit_request", {"request_key": "two", "description": "送水", "room": "102"})).isError
        async with httpx.AsyncClient(trust_env=False) as http:
            denied = await http.post(hotel["desk"]["url"], headers=hotel["room"]["headers"], json={})
            assert denied.status_code == 401
            cross_session = await http.post(hotel["desk"]["url"], headers={**hotel["desk"]["headers"],
                "Mcp-Session-Id": session_id(), "Accept": "application/json, text/event-stream"},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            assert cross_session.status_code in (400, 404)
            assert "session" in cross_session.text.lower()
    for name in ("neighbor", "other"):
        async with client(hotel[name]) as (session, _):
            assert result(await session.call_tool("hotel_list_requests")) == {"requests": [], "next_cursor": None}
            inbox = await session.read_resource(AnyUrl("business://events?after=0"))
            assert json.loads(inbox.contents[0].text)["events"] == []
            assert (await session.call_tool("hotel_accept_request", {"request_id": order["id"]})).isError


@pytest.mark.asyncio
async def test_failed_delivery_retains_cursor(hotel, tmp_path):
    cursors = CursorStore(tmp_path / "cursor.db")
    async with client(hotel["room"]) as (session, _):
        result(await session.call_tool("hotel_submit_request", {"request_key": "one", "description": "送水"}))
    async def offline(payload):
        raise RuntimeError("device offline")
    with pytest.raises(BaseExceptionGroup) as failure:
        await consume(hotel["desk"], cursors, "desk", offline)
    assert failure.value.subgroup(RuntimeError) is not None
    assert cursors.read("desk") == 0
    events = asyncio.Queue()
    task = asyncio.create_task(consume(hotel["desk"], cursors, "desk", events.put))
    try:
        await asyncio.wait_for(events.get(), 3)
        await asyncio.sleep(.05)
        assert cursors.read("desk") > 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_transaction_idempotency_state_machine_and_pagination(tmp_path):
    path = tmp_path / "hotel.db"
    store = HotelStore(path)
    room, desk = Principal("a", "room", "101"), Principal("a", "frontdesk")
    first = store.submit(room, "one", "送水")
    assert HotelStore(path).submit(room, "one", "送水")["id"] == first["id"]
    with pytest.raises(ValueError):
        store.submit(room, "one", "送毛巾")
    with pytest.raises(ValueError):
        store.transition(desk, first["id"], "completed")
    store.transition(desk, first["id"], "accepted")
    store.transition(desk, first["id"], "accepted")
    assert len(store.inbox(room)["events"]) == 1
    for n in range(52):
        store.submit(room, str(n), "送水")
    page1 = store.inbox(desk)
    page2 = store.inbox(desk, page1["next_cursor"])
    assert len(page1["events"]) == 50 and page1["has_more"]
    assert len(page2["events"]) == 3 and not page2["has_more"]
