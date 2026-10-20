"""Customer demo path over actual MCP HTTP on a random loopback port."""
import asyncio
import json
import socket

import pytest
import pytest_asyncio
import uvicorn
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl

from hotel_mcp.demo import init_sales_demo
from hotel_mcp.server import create_app
from hotel_mcp.store import HotelStore, Principal
from tests.test_hotel import client, result
from tests.test_repair_protocol import error


@pytest_asyncio.fixture
async def sales_server(tmp_path):
    directory = tmp_path / "sales"
    init_sales_demo(directory, "2026-10-18")
    store = HotelStore(directory / "hotel.sqlite3")
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    config["rooms"] = [{"hotel": "demo-central", "room": "101"}]
    store.configure_rooms(config["rooms"])
    stay = store.start_stay(Principal("demo-central", "frontdesk"), "101", "2099-01-01", "checkin")["stay"]
    config["principals"].append({"id": "room", "hotel": "demo-central", "role": "room", "room": "101",
                                 "stay_id": stay["id"], "token": "r" * 40})
    app = create_app(config, store.path)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.02)
        assert server.started
        yield {row["id"]: {"url": f"http://127.0.0.1:{port}/{row['id']}/mcp",
                            "headers": {"Authorization": "Bearer " + row["token"]}} for row in config["principals"]}
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)
        sock.close()


@pytest.mark.asyncio
async def test_sales_mcp_demo_list_switch_compare_filter_and_reconnect(sales_server):
    async with client(sales_server["sales-manager"]) as (manager, _):
        assert {t.name for t in (await manager.list_tools()).tools} == {"hotel_list_stores", "hotel_query_sales", "hotel_resolve_entities"}
        listing = result(await manager.call_tool("hotel_list_stores", {}))
        assert len(listing["stores"]) == 3 and listing["is_demo"]
        args = {"date_from": "2026-10-12", "date_to": "2026-10-18"}
        report = result(await manager.call_tool("hotel_query_sales", args))
        assert report["totals"] == {"sales_amount_minor": 2452800, "order_count": 84}
        assert report["data_source"] == "demo" and "演示" in report["display_notice"]
        switched = result(await manager.call_tool("hotel_query_sales", {**args, "hotel_ids": ["demo-central"]}))
        assert switched["totals"]["order_count"] == 28
        meal = result(await manager.call_tool("hotel_query_sales", {**args, "category": "dining"}))
        assert meal["totals"]["order_count"] == 21
        empty = result(await manager.call_tool("hotel_query_sales", {"date_from": "2026-10-19", "date_to": "2026-10-19"}))
        assert empty["totals"]["sales_amount_minor"] == 0
        for bad in ({}, {**args, "date_from": "yesterday"}, {**args, "hotel_ids": []}, {**args, "role": "frontdesk"}):
            error(await manager.call_tool("hotel_query_sales", bad), "VALIDATION_ERROR")
    async with client(sales_server["sales-manager"]) as (manager, _):
        assert result(await manager.call_tool("hotel_query_sales", args)) == report


@pytest.mark.asyncio
async def test_sales_mcp_role_and_store_isolation_and_quotes_are_not_income(sales_server):
    args = {"date_from": "2026-10-12", "date_to": "2026-10-18"}
    async with client(sales_server["central-manager"]) as (manager, _):
        assert len(result(await manager.call_tool("hotel_list_stores"))["stores"]) == 1
        error(await manager.call_tool("hotel_query_sales", {**args, "hotel_ids": ["demo-airport"]}), "FORBIDDEN")
        assert (await manager.list_resources()).resources == []
        for name in ("hotel_list_requests", "hotel_start_stay", "hotel_cancel_request", "hotel_quote_stay_extension"):
            error(await manager.call_tool(name, {}), "FORBIDDEN")
        with pytest.raises(McpError):
            await manager.read_resource(AnyUrl("business://events"))
        with pytest.raises(McpError):
            await manager.subscribe_resource(AnyUrl("business://events"))
    async with client(sales_server["frontdesk-demo-central"]) as (desk, _), client(sales_server["room"]) as (room, _):
        assert "hotel_query_sales" not in {t.name for t in (await room.list_tools()).tools}
        error(await room.call_tool("hotel_query_sales", args), "FORBIDDEN")
        error(await desk.call_tool("hotel_query_sales", {**args, "hotel_ids": ["demo-central", "demo-riverside"]}), "FORBIDDEN")
        before = result(await desk.call_tool("hotel_query_sales", args))
        assert before["hotel_ids"] == ["demo-central"]
        order = result(await room.call_tool("hotel_submit_stay_extension", {"target_checkout_date": "2099-01-02", "request_key": "renew"}))["request"]
        result(await desk.call_tool("hotel_quote_stay_extension", {"request_id": order["id"], "total_amount_minor": 39800,
                     "pricing_note": "人工核实费用", "expected_version": 1, "request_key": "quote"}))
        assert result(await desk.call_tool("hotel_query_sales", args)) == before
        assert (await desk.list_resources()).resources  # Existing work-order inbox remains available.
