import asyncio
import json
import socket
import subprocess
import sys

import httpx
import pytest
import pytest_asyncio
import uvicorn

from hotel_mcp.server import create_app
from tests.protocol_client import CursorStore, consume
from tests.test_hotel import client, result
from tests.test_repair_protocol import error
from tests.test_services import services


@pytest_asyncio.fixture
async def service_server(services):
    store, room, desk, summary, config = services
    app = create_app(config, store.path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.02)
        assert server.started
        connections = {r["id"]: {"url": base + "/" + r["id"] + "/mcp", "headers": {"Authorization": "Bearer " + r["token"]}}
                       for r in config["principals"]}
        yield base, connections, services
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)
        sock.close()


@pytest.mark.asyncio
async def test_room_mcp_to_frontdesk_web_to_room_notification_and_replay(service_server, tmp_path):
    base, connections, (store, room, desk, summary, _) = service_server
    guest_config = connections[room.principal_id]
    events = asyncio.Queue()
    consumer = asyncio.create_task(consume(guest_config, CursorStore(tmp_path / "cursor.db"), "room", events.put, poll_seconds=60))
    try:
        await asyncio.sleep(.4)
        async with client(guest_config) as (guest, _), httpx.AsyncClient(base_url=base, trust_env=False) as http:
            menu = result(await guest.call_tool("hotel_list_menu"))["items"]
            order = result(await guest.call_tool("hotel_submit_meal_order", {"items": [{"item_id": menu[0]["id"], "quantity": 1}], "request_key": "voice-meal"}))["request"]
            desk_headers = connections[desk.principal_id]["headers"]
            endpoint = f"/api/{desk.principal_id}/tools/hotel_process_request"
            data = {"request_id": order["id"], "action": "accept", "note": "已核实厨房可制作", "expected_version": 1, "request_key": "web-accept"}
            accepted = await http.post(endpoint, headers=desk_headers, json=data)
            assert accepted.status_code == 200 and accepted.json()["request"]["status"] == "accepted"
            event = await asyncio.wait_for(events.get(), 3)
            assert event["event_type"] == "hotel.meal.accepted" and event["request_id"] == order["id"]
            replay = await http.post(endpoint, headers=desk_headers, json=data)
            assert replay.json()["replayed"]
            assert store.latest_event_seq(room) == event["seq"]
            completed = await http.post(endpoint, headers=desk_headers, json={**data, "action": "complete", "expected_version": 2, "request_key": "web-complete", "note": "已送达客房，线下结算"})
            assert completed.json()["request"]["status"] == "completed"
            assert (await asyncio.wait_for(events.get(), 3))["event_type"] == "hotel.meal.completed"
            assert result(await guest.call_tool("hotel_get_request", {"request_id": order["id"]}))["request"]["status"] == "completed"
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gym", "meeting"])
async def test_booking_mcp_apply_web_confirm_conflict_and_cancel(service_server, kind):
    base, connections, (store, room, desk, summary, _) = service_server
    async with client(connections[room.principal_id]) as (guest, _), httpx.AsyncClient(base_url=base, trust_env=False) as http:
        slot = result(await guest.call_tool("hotel_list_booking_slots", {"kind": kind, "date": summary["booking_date"]}))["slots"][-1]
        args = {"slot_id": slot["id"], "participants": 4}
        first = result(await guest.call_tool("hotel_submit_booking", {**args, "request_key": "first"}))["request"]
        second = result(await guest.call_tool("hotel_submit_booking", {**args, "request_key": "second"}))["request"]
        endpoint = f"/api/{desk.principal_id}/tools/hotel_process_request"
        headers = connections[desk.principal_id]["headers"]
        args = {"action": "accept", "note": "确认可用", "expected_version": 1, "request_key": "confirm"}
        assert (await http.post(endpoint, headers=headers, json={**args, "request_id": first["id"]})).status_code == 200
        conflict = await http.post(endpoint, headers=headers, json={**args, "request_id": second["id"], "request_key": "confirm-2"})
        assert conflict.status_code == 409 and conflict.json()["code"] == "RESOURCE_UNAVAILABLE"
        error(await guest.call_tool("hotel_cancel_request", {"request_id": first["id"], "expected_version": 2, "reason": "取消", "request_key": "guest-cancel"}), "INVALID_TRANSITION")
        cancelled = await http.post(f"/api/{desk.principal_id}/tools/hotel_cancel_request", headers=headers,
                                    json={"request_id": first["id"], "expected_version": 2, "reason": "已确认取消", "request_key": "cancel"})
        assert cancelled.status_code == 200
        assert (await http.post(endpoint, headers=headers, json={**args, "request_id": second["id"], "request_key": "confirm-2"})).status_code == 200


@pytest.mark.asyncio
async def test_web_authorization_schema_static_assets_and_no_credentials_in_public_config(service_server):
    base, connections, (store, room, desk, summary, config) = service_server
    async with httpx.AsyncClient(base_url=base, trust_env=False) as http:
        page = await http.get("/admin/")
        assert page.status_code == 200 and "酒店服务工作台" in page.text
        for asset in ("app.js", "style.css"):
            assert (await http.get("/admin/assets/" + asset)).status_code == 200
        public = await http.get("/admin/config")
        assert not any(r["token"] in page.text + public.text for r in config["principals"])
        assert not public.json()["analytics"]["enabled"]
        assert (await http.get(f"/api/{desk.principal_id}/session")).status_code == 401
        session = await http.get(f"/api/{desk.principal_id}/session", headers=connections[desk.principal_id]["headers"])
        assert session.status_code == 200 and session.json()["principal"]["role"] == "frontdesk"
        row = store.query_requests(room)["requests"][0]
        cases = [("sales-manager", "hotel_query_requests", {}), (room.principal_id, "hotel_process_request", {}),
                 (desk.principal_id, "hotel_submit_booking", {}), (desk.principal_id, "unknown_tool", {})]
        for ident, tool, args in cases:
            response = await http.post(f"/api/{ident}/tools/{tool}", headers=connections[ident]["headers"], json=args)
            assert response.status_code == 403 and response.json()["code"] == "FORBIDDEN"
        other = "frontdesk-demo-airport"
        denied = await http.post(f"/api/{other}/tools/hotel_get_request", headers=connections[other]["headers"], json={"request_id": row["id"]})
        assert denied.status_code == 404
        bad = await http.post(f"/api/{desk.principal_id}/tools/hotel_process_request", headers=connections[desk.principal_id]["headers"], json={})
        assert bad.status_code == 400 and bad.json()["code"] == "VALIDATION_ERROR"
        malformed = await http.post(f"/api/{desk.principal_id}/tools/hotel_query_requests", headers=connections[desk.principal_id]["headers"], content="{")
        assert malformed.status_code == 400


def test_full_demo_cli_and_no_overwrite(tmp_path):
    root = tmp_path / "full"
    command = [sys.executable, "-m", "hotel_mcp", "--init-demo", str(root), "--demo-end-date", "2026-10-18"]
    created = subprocess.run(command, capture_output=True, text=True)
    assert created.returncode == 0, created.stderr
    assert json.loads(created.stdout)["request_count"] == 15
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    assert len([r for r in config["principals"] if r["role"] == "room"]) == 6
    assert all(r["token"] not in created.stdout + created.stderr for r in config["principals"])
    assert subprocess.run(command, capture_output=True).returncode != 0
