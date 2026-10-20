"""Real HTTP MCP renewal acceptance, with only loopback random-port fixtures."""
import asyncio

import pytest
from pydantic import AnyUrl

from tests.test_hotel import hotel, client, result
from tests.test_repair_protocol import error
from tests.protocol_client import CursorStore, consume


@pytest.mark.asyncio
async def test_extension_http_success_notifications_reconnect_and_replay(hotel, tmp_path):
    desk_events, room_events = asyncio.Queue(), asyncio.Queue()
    cursor_path = tmp_path / "extension-cursors.db"
    cursors = CursorStore(cursor_path)
    desk_consumer = asyncio.create_task(consume(hotel["desk"], cursors, "desk", desk_events.put, poll_seconds=60))
    room_consumer = asyncio.create_task(consume(hotel["room"], cursors, "room", room_events.put, poll_seconds=60))
    try:
        await asyncio.sleep(.4)
        async with client(hotel["room"]) as (room, _), client(hotel["desk"]) as (desk, _):
            room_tools = {t.name for t in (await room.list_tools()).tools}
            desk_tools = {t.name for t in (await desk.list_tools()).tools}
            assert {"hotel_submit_stay_extension", "hotel_confirm_stay_extension", "hotel_list_stay_extension_quotes"} <= room_tools
            assert "hotel_finish_stay_extension" not in room_tools
            assert {"hotel_quote_stay_extension", "hotel_begin_stay_extension", "hotel_finish_stay_extension", "hotel_reject_stay_extension"} <= desk_tools
            assert "hotel_confirm_stay_extension" not in desk_tools
            initial_stay = result(await room.call_tool("hotel_get_current_stay"))["stay"]
            order = result(await room.call_tool("hotel_submit_stay_extension", {"target_checkout_date": "2099-01-03", "request_key": "extension"}))["request"]
            assert (await asyncio.wait_for(desk_events.get(), 3))["event_type"] == "hotel.stay_extension.submitted"
            offer_args = {"request_id": order["id"], "total_amount_minor": 76000, "pricing_note": "两晚含早餐，线下由前台办理", "expected_version": 1, "request_key": "quote"}
            offered = result(await desk.call_tool("hotel_quote_stay_extension", offer_args))["request"]
            qid = offered["extension"]["current_quote_id"]
            notification = await asyncio.wait_for(room_events.get(), 3)
            assert notification["event_type"] == "hotel.stay_extension.quoted" and notification["total_amount_minor"] == 76000
            assert "尚未办理" in notification["speech_text"]
            quotes = result(await room.call_tool("hotel_list_stay_extension_quotes", {"request_id": order["id"]}))["quotes"]
            assert quotes[0]["id"] == qid and "actor_principal_id" not in quotes[0]
            confirm_args = {"request_id": order["id"], "quote_id": qid, "expected_version": 2, "request_key": "confirm"}
            confirmed = result(await room.call_tool("hotel_confirm_stay_extension", confirm_args))["request"]
            assert confirmed["status"] == "confirmed"
            assert (await asyncio.wait_for(desk_events.get(), 3))["event_type"] == "hotel.stay_extension.confirmed"
            assert result(await room.call_tool("hotel_confirm_stay_extension", confirm_args))["replayed"]
            begun = result(await desk.call_tool("hotel_begin_stay_extension", {**confirm_args, "expected_version": 3, "request_key": "begin", "note": "已核实可续住，开始办理"}))["request"]
            assert begun["status"] == "processing"
            assert (await asyncio.wait_for(room_events.get(), 3))["event_type"] == "hotel.stay_extension.processing"
            assert result(await room.call_tool("hotel_get_current_stay"))["stay"] == initial_stay
            error(await desk.call_tool("hotel_end_stay", {"stay_id": initial_stay["id"], "expected_version": 1, "reason": "退房", "request_key": "checkout"}), "STAY_HAS_PROCESSING_EXTENSION")
            room_consumer.cancel()
            await asyncio.gather(room_consumer, return_exceptions=True)
            finish_args = {**confirm_args, "expected_version": 4, "request_key": "finish", "outcome": "success", "note": "前台已完成续房登记", "result_reference": "manual-001"}
            finished = result(await desk.call_tool("hotel_finish_stay_extension", finish_args))
            assert finished["request"]["status"] == "completed" and finished["stay"]["expected_checkout_date"] == "2099-01-03"
            room_consumer = asyncio.create_task(consume(hotel["room"], CursorStore(cursor_path), "room", room_events.put, poll_seconds=60))
            event = await asyncio.wait_for(room_events.get(), 3)
            assert event["event_type"] == "hotel.stay_extension.completed" and "2099-01-03" in event["speech_text"]
            assert result(await desk.call_tool("hotel_finish_stay_extension", finish_args))["replayed"]
            assert result(await room.call_tool("hotel_get_current_stay"))["stay"]["version"] == 2
            results = result(await desk.call_tool("hotel_query_requests", {"request_type": "stay_extension", "statuses": ["completed"]}))["requests"]
            assert [r["id"] for r in results] == [order["id"]]
            actions = result(await room.call_tool("hotel_list_request_actions", {"request_id": order["id"]}))["actions"]
            assert actions[0]["data"]["quote_id"] == qid and actions[0]["data"]["outcome"] == "success"
            await asyncio.sleep(.1)
            assert room_events.empty() and desk_events.empty()
    finally:
        desk_consumer.cancel()
        room_consumer.cancel()
        await asyncio.gather(desk_consumer, room_consumer, return_exceptions=True)


@pytest.mark.asyncio
async def test_extension_http_requote_permissions_and_manual_failure(hotel):
    async with client(hotel["room"]) as (room, _), client(hotel["desk"]) as (desk, _), client(hotel["neighbor"]) as (neighbor, _), client(hotel["other"]) as (other, _):
        for bad in ({"target_checkout_date": "2099-01-03", "request_key": "one", "room": "102"},
                    {"target_checkout_date": "2099-02-29", "request_key": "one"}):
            error(await room.call_tool("hotel_submit_stay_extension", bad), "VALIDATION_ERROR")
        order = result(await room.call_tool("hotel_submit_stay_extension", {"target_checkout_date": "2099-01-03", "request_key": "one"}))["request"]
        offer = {"request_id": order["id"], "total_amount_minor": 76000, "pricing_note": "人工核实价格", "expected_version": 1, "request_key": "quote"}
        error(await room.call_tool("hotel_quote_stay_extension", offer), "FORBIDDEN")
        error(await other.call_tool("hotel_quote_stay_extension", offer), "NOT_FOUND")
        for bad in ({**offer, "total_amount_minor": 7.6}, {**offer, "currency": "USD"}, {**offer, "valid_for_minutes": 0}):
            error(await desk.call_tool("hotel_quote_stay_extension", bad), "VALIDATION_ERROR")
        quoted = result(await desk.call_tool("hotel_quote_stay_extension", offer))["request"]
        old_id = quoted["extension"]["current_quote_id"]
        cargs = {"request_id": order["id"], "quote_id": old_id, "expected_version": 2, "request_key": "confirm"}
        error(await neighbor.call_tool("hotel_confirm_stay_extension", cargs), "NOT_FOUND")
        error(await desk.call_tool("hotel_confirm_stay_extension", cargs), "FORBIDDEN")
        result(await room.call_tool("hotel_confirm_stay_extension", cargs))
        revised = result(await desk.call_tool("hotel_quote_stay_extension", {**offer, "expected_version": 3, "request_key": "revise", "total_amount_minor": 80000, "change_reason": "原报价遗漏早餐费用"}))["request"]
        assert revised["extension"]["confirmed_quote_id"] is None
        error(await room.call_tool("hotel_confirm_stay_extension", {**cargs, "expected_version": 4, "request_key": "old"}), "QUOTE_SUPERSEDED")
        new_id = revised["extension"]["current_quote_id"]
        result(await room.call_tool("hotel_confirm_stay_extension", {**cargs, "quote_id": new_id, "expected_version": 4, "request_key": "new"}))
        result(await desk.call_tool("hotel_begin_stay_extension", {**cargs, "quote_id": new_id, "expected_version": 5, "request_key": "begin", "note": "开始人工办理"}))
        error(await desk.call_tool("hotel_process_request", {"request_id": order["id"], "action": "complete", "expected_version": 6, "request_key": "bypass", "note": "不能绕过"}), "INVALID_TRANSITION")
        failed = result(await desk.call_tool("hotel_finish_stay_extension", {**cargs, "quote_id": new_id, "expected_version": 6, "request_key": "failed", "outcome": "failed", "note": "人工复核房态冲突，未办理"}))
        assert failed["request"]["status"] == "failed"
        assert failed["stay"]["expected_checkout_date"] == "2099-01-01" and failed["stay"]["version"] == 1


@pytest.mark.asyncio
async def test_extension_http_cancel_reject_and_checkout_cleanup(hotel):
    async with client(hotel["room"]) as (room, _), client(hotel["desk"]) as (desk, _):
        stay = result(await room.call_tool("hotel_get_current_stay"))["stay"]
        for action in ("cancel", "reject", "checkout"):
            order = result(await room.call_tool("hotel_submit_stay_extension", {"target_checkout_date": "2099-01-03", "request_key": action}))["request"]
            if action == "cancel":
                ended = result(await room.call_tool("hotel_cancel_request", {"request_id": order["id"], "expected_version": 1, "request_key": "cancel", "reason": "行程改变"}))
                assert ended["request"]["status"] == "cancelled"
            elif action == "reject":
                ended = result(await desk.call_tool("hotel_reject_stay_extension", {"request_id": order["id"], "expected_version": 1, "request_key": "reject", "reason": "无可续房态"}))
                assert ended["request"]["status"] == "rejected"
            else:
                ended = result(await desk.call_tool("hotel_end_stay", {"stay_id": stay["id"], "expected_version": 1, "request_key": "end", "reason": "退房"}))
                assert ended["cancelled_extension_count"] == 1 and ended["open_request_count"] == 0
                error(await room.call_tool("hotel_get_request", {"request_id": order["id"]}), "STAY_INACTIVE")
                detail = result(await desk.call_tool("hotel_get_request", {"request_id": order["id"]}))["request"]
                assert detail["status"] == "cancelled"
