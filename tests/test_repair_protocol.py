"""Acceptance over real loopback HTTP MCP sessions; no running service access."""
import asyncio
import json

import pytest
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from tests.test_hotel import client, hotel, result
from tests.protocol_client import CursorStore, consume


def error(value, code):
    assert value.isError
    assert json.loads(value.content[0].text)["code"] == code


@pytest.mark.asyncio
async def test_repair_protocol_end_to_end_subscription_reconnect_and_checkout(hotel, tmp_path):
    desk_events, room_events = asyncio.Queue(), asyncio.Queue()
    cursor_path = tmp_path / "repair-cursors.db"
    cursors = CursorStore(cursor_path)
    desk_consumer = asyncio.create_task(consume(hotel["desk"], cursors, "desk", desk_events.put, poll_seconds=60))
    room_consumer = asyncio.create_task(consume(hotel["room"], cursors, "room", room_events.put, poll_seconds=60))
    try:
        await asyncio.sleep(.4)
        async with client(hotel["room"]) as (room, _), client(hotel["desk"]) as (desk, _):
            room_tools = {t.name for t in (await room.list_tools()).tools}
            desk_tools = {t.name for t in (await desk.list_tools()).tools}
            assert "hotel_submit_repair" in room_tools and "hotel_process_request" not in room_tools
            assert {"hotel_process_request", "hotel_start_stay", "hotel_end_stay"} <= desk_tools
            stay = result(await room.call_tool("hotel_get_current_stay"))["stay"]
            submitted = result(await room.call_tool("hotel_submit_repair", {
                "request_key": "repair-turn-1", "category": "air_conditioning", "description": "空调不制冷", "location": "卧室"}))
            order = submitted["request"]
            event = await asyncio.wait_for(desk_events.get(), 3)
            assert event["event_type"] == "hotel.repair.submitted" and event["request_id"] == order["id"]
            accepted_args = {"request_id": order["id"], "action": "accept", "note": "十五分钟内检查", "expected_version": 1, "request_key": "accept"}
            accepted = result(await desk.call_tool("hotel_process_request", accepted_args))
            assert accepted["request"]["status"] == "accepted"
            accepted_event = await asyncio.wait_for(room_events.get(), 3)
            assert accepted_event["event_type"] == "hotel.repair.accepted"
            assert result(await desk.call_tool("hotel_process_request", accepted_args))["replayed"]
            error(await room.call_tool("hotel_cancel_request", {"request_id": order["id"], "reason": "取消", "expected_version": 2, "request_key": "cancel"}), "INVALID_TRANSITION")
            result(await desk.call_tool("hotel_process_request", {**accepted_args, "action": "add_note", "note": "已到场检查", "expected_version": 2, "request_key": "note"}))
            assert (await asyncio.wait_for(room_events.get(), 3))["event_type"] == "hotel.repair.updated"
            # Offline during completion: resumed consumer must read the durable event.
            room_consumer.cancel()
            await asyncio.gather(room_consumer, return_exceptions=True)
            result(await desk.call_tool("hotel_process_request", {**accepted_args, "action": "complete", "note": "更换部件后恢复制冷", "expected_version": 3, "request_key": "complete"}))
            room_consumer = asyncio.create_task(consume(hotel["room"], CursorStore(cursor_path), "room", room_events.put, poll_seconds=60))
            assert (await asyncio.wait_for(room_events.get(), 3))["event_type"] == "hotel.repair.completed"
            latest = result(await room.call_tool("hotel_get_request", {"request_id": order["id"]}))["request"]
            assert latest["status"] == "completed" and latest["version"] == 4
            history = result(await room.call_tool("hotel_list_request_actions", {"request_id": order["id"]}))
            assert len(history["actions"]) == 4
            filtered = result(await desk.call_tool("hotel_query_requests", {"request_type": "repair", "statuses": ["completed"], "room": "101"}))
            assert [r["id"] for r in filtered["requests"]] == [order["id"]]
            ended = result(await desk.call_tool("hotel_end_stay", {"stay_id": stay["id"], "expected_version": 1, "reason": "退房", "request_key": "end"}))
            assert ended["open_request_count"] == 0
            # Already-established sessions must lose access, including a newly opened session.
            error(await room.call_tool("hotel_get_request", {"request_id": order["id"]}), "STAY_INACTIVE")
            with pytest.raises(McpError):
                await room.read_resource(AnyUrl("business://events?after=0"))
            with pytest.raises(McpError):
                await room.subscribe_resource(AnyUrl("business://events"))
            async with client(hotel["room"]) as (fresh, _):
                error(await fresh.call_tool("hotel_get_current_stay"), "STAY_INACTIVE")
    finally:
        desk_consumer.cancel()
        room_consumer.cancel()
        await asyncio.gather(desk_consumer, room_consumer, return_exceptions=True)


@pytest.mark.asyncio
async def test_protocol_schema_permissions_exits_and_stay_management(hotel):
    async with client(hotel["room"]) as (room, _), client(hotel["desk"]) as (desk, _), client(hotel["neighbor"]) as (neighbor, _):
        args = {"request_key": "one", "category": "network", "description": "无法联网"}
        for invalid in ({**args, "room": "102"}, {**args, "category": "made_up"}, {**args, "description": " "},
                        {**args, "urgency": 1}, {"request_key": "one"}):
            error(await room.call_tool("hotel_submit_repair", invalid), "VALIDATION_ERROR")
        error(await room.call_tool("hotel_start_stay", {"room": "103", "expected_checkout_date": "2099-01-01", "request_key": "start"}), "FORBIDDEN")
        order = result(await room.call_tool("hotel_submit_repair", args))["request"]
        error(await neighbor.call_tool("hotel_get_request", {"request_id": order["id"]}), "NOT_FOUND")
        error(await neighbor.call_tool("hotel_cancel_request", {"request_id": order["id"], "reason": "取消", "expected_version": 1, "request_key": "cancel"}), "NOT_FOUND")
        error(await room.call_tool("hotel_query_requests", {"room": "102"}), "VALIDATION_ERROR")
        error(await desk.call_tool("hotel_accept_request", {"request_id": order["id"]}), "VALIDATION_ERROR")
        for key, terminal in (("cancel", "cancelled"), ("reject", "rejected"), ("unresolved", "closed_unresolved")):
            item = result(await room.call_tool("hotel_submit_repair", {**args, "request_key": key}))["request"]
            process = {"request_id": item["id"], "expected_version": 1, "request_key": key, "note": "已确认原因及后续安排"}
            if terminal == "cancelled":
                final = result(await room.call_tool("hotel_cancel_request", {"request_id": item["id"], "reason": "取消", "expected_version": 1, "request_key": key}))
            elif terminal == "rejected":
                final = result(await desk.call_tool("hotel_process_request", {**process, "action": "reject"}))
            else:
                result(await desk.call_tool("hotel_process_request", {**process, "action": "accept"}))
                final = result(await desk.call_tool("hotel_process_request", {**process, "action": "close_unresolved", "expected_version": 2, "request_key": "close"}))
            assert final["request"]["status"] == terminal
        started = result(await desk.call_tool("hotel_start_stay", {"room": "103", "expected_checkout_date": "2099-01-01", "request_key": "start"}))
        assert started["stay"]["status"] == "active"
        assert "token" not in json.dumps(started)
        error(await desk.call_tool("hotel_start_stay", {"room": "103", "expected_checkout_date": "2099-01-01", "request_key": "duplicate"}), "INVALID_TRANSITION")


@pytest.mark.asyncio
async def test_no_resource_notifications_to_unrelated_room(hotel):
    # Unlike polling a resource, this observes actual server-originated notifications.
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    from tests.protocol_client import http_client_factory

    messages = asyncio.Queue()
    async with streamablehttp_client(**hotel["neighbor"], httpx_client_factory=http_client_factory) as (read, write, _):
        async with ClientSession(read, write, message_handler=messages.put) as neighbor:
            await neighbor.initialize()
            await neighbor.subscribe_resource(AnyUrl("business://events"))
            async with client(hotel["room"]) as (room, _), client(hotel["desk"]) as (desk, _):
                order = result(await room.call_tool("hotel_submit_repair", {"request_key": "one", "category": "other", "description": "设施损坏"}))["request"]
                result(await desk.call_tool("hotel_process_request", {"request_id": order["id"], "action": "accept", "note": "安排", "expected_version": 1, "request_key": "accept"}))
                await asyncio.sleep(.1)
                assert messages.empty()
