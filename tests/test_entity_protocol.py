"""MCP text-to-entity-to-sales demonstration; simulated user clarification."""
import pytest

from tests.test_hotel import client, result
from tests.test_repair_protocol import error
from tests.test_sales_protocol import sales_server


@pytest.mark.asyncio
async def test_mcp_resolve_clarify_select_and_query_sales(sales_server):
    args = {"date_from": "2026-10-12", "date_to": "2026-10-18"}
    async with client(sales_server["sales-manager"]) as (session, _):
        alias = result(await session.call_tool("hotel_resolve_entities", {"query": "中心门店"}))
        assert alias["resolved_id"] == "demo-central"
        report = result(await session.call_tool("hotel_query_sales", {**args, "hotel_ids": [alias["resolved_id"]]}))
        assert report["is_demo"] and report["totals"]["sales_amount_minor"] == 789600
        for query, expected_kind in (("江盼店", "asr"), ("江盘店", "similar")):
            unclear = result(await session.call_tool("hotel_resolve_entities", {"query": query}))
            assert unclear["resolved_id"] is None and unclear["requires_confirmation"]
            assert unclear["candidates"][0]["match_type"] == expected_kind
            assert "江畔店" in unclear["clarification_question"]
            # Simulate user answering "是江畔店" before using the candidate ID.
            confirmed = result(await session.call_tool("hotel_resolve_entities", {"query": "江畔店"}))
            report = result(await session.call_tool("hotel_query_sales", {**args, "hotel_ids": [confirmed["resolved_id"]]}))
            assert report["hotel_ids"] == ["demo-riverside"] and report["totals"]["sales_amount_minor"] == 817600
        ambiguous = result(await session.call_tool("hotel_resolve_entities", {"query": "市区店"}))
        assert ambiguous["status"] == "needs_clarification" and len(ambiguous["candidates"]) == 2
        assert ambiguous["resolved_id"] is None
        # Simulate an explicit choice of the second candidate, never a first-item default.
        choice = ambiguous["candidates"][1]["entity_id"]
        chosen = result(await session.call_tool("hotel_resolve_entities", {"query": choice}))
        report = result(await session.call_tool("hotel_query_sales", {**args, "hotel_ids": [chosen["resolved_id"]]}))
        assert report["hotel_ids"] == [choice]
        absent = result(await session.call_tool("hotel_resolve_entities", {"query": "火星度假村"}))
        assert absent["status"] == "not_found" and absent["next_step"] == "ask_user"
        # Sales still requires authorized IDs; names cannot silently select all stores.
        error(await session.call_tool("hotel_query_sales", {**args, "hotel_ids": ["江盼店"]}), "FORBIDDEN")
    async with client(sales_server["sales-manager"]) as (session, _):
        assert result(await session.call_tool("hotel_resolve_entities", {"query": "市区店"})) == ambiguous


@pytest.mark.asyncio
async def test_mcp_resolver_scope_and_invalid_arguments(sales_server):
    for endpoint in ("central-manager", "frontdesk-demo-central"):
        async with client(sales_server[endpoint]) as (session, _):
            resolved = result(await session.call_tool("hotel_resolve_entities", {"query": "市区店"}))
            assert resolved["resolved_id"] == "demo-central" and len(resolved["candidates"]) == 1
            assert result(await session.call_tool("hotel_resolve_entities", {"query": "江畔店"}))["status"] == "not_found"
            for bad in ({}, {"query": ""}, {"query": "!!!"}, {"query": "a" * 81},
                        {"query": "中心店", "entity_type": "room"}, {"query": "中心店", "hotel_ids": ["demo-riverside"]}):
                error(await session.call_tool("hotel_resolve_entities", bad), "VALIDATION_ERROR")
    async with client(sales_server["room"]) as (session, _):
        assert "hotel_resolve_entities" not in {t.name for t in (await session.list_tools()).tools}
        error(await session.call_tool("hotel_resolve_entities", {"query": "中心店"}), "FORBIDDEN")
