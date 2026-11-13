"""Scoped alias resolution, ambiguity and migration acceptance on temporary DBs."""
import sqlite3

import pytest

from hotel_mcp.demo import DEMO_ALIASES
from hotel_mcp.domain import Principal
from hotel_mcp.server import create_app
from hotel_mcp.store import HotelStore
from tests.test_business import assert_error
from tests.test_sales import sales
from tests.schema_helpers import remove_service_tables


@pytest.mark.parametrize("query,kind", [
    ("demo-central", "exact_id"), ("中心店（演示）", "exact_name"),
    (" 中心门店 ", "alias"), ("中 心 店！", "alias"),
])
def test_unique_names_aliases_and_normalization_resolve(sales, query, kind):
    store, manager = sales
    result = store.entities.resolve(manager, query)
    assert result["status"] == "resolved" and result["resolved_id"] == "demo-central"
    assert not result["requires_confirmation"] and result["clarification_question"] is None
    assert result["candidates"][0]["match_type"] == kind
    assert result["next_step"] == "query_sales_with_id"
    assert store.sales.query(manager, "2026-10-12", "2026-10-18", [result["resolved_id"]])["totals"]["order_count"] == 28


@pytest.mark.parametrize("query,kind", [("江盼店", "asr"), ("江盘店", "similar"), ("中心", "similar")])
def test_asr_and_fuzzy_suggestions_always_require_confirmation(sales, query, kind):
    store, manager = sales
    result = store.entities.resolve(manager, query)
    assert result["status"] == "needs_clarification" and result["resolved_id"] is None
    assert result["requires_confirmation"] and result["next_step"] == "ask_user"
    assert len(result["candidates"]) == 1 and result["candidates"][0]["match_type"] == kind
    assert "吗" in result["clarification_question"]


def test_shared_alias_and_duplicate_official_names_never_pick_first(sales):
    store, manager = sales
    result = store.entities.resolve(manager, "市区店")
    assert result["status"] == "needs_clarification" and result["resolved_id"] is None
    assert {c["entity_id"] for c in result["candidates"]} == {"demo-central", "demo-riverside"}
    assert "中心店" in result["clarification_question"] and "江畔店" in result["clarification_question"]
    with store.connect() as db:
        db.execute("UPDATE stores SET name='同名门店' WHERE hotel IN ('demo-central','demo-airport')")
    same_name = store.entities.resolve(manager, "同名门店")
    assert same_name["status"] == "needs_clarification" and len(same_name["candidates"]) == 2
    assert all(c["entity_id"] not in same_name["clarification_question"] for c in same_name["candidates"])
    # An alias that collides with a formal name is also ambiguous.
    store.entities.configure_aliases([{"hotel": "demo-riverside", "alias": "同名门店"}])
    assert len(store.entities.resolve(manager, "同名门店")["candidates"]) == 3


def test_resolution_filters_permissions_before_matching(sales):
    store, _ = sales
    limited = Principal("", "manager", allowed_hotels=("demo-central",))
    desk = Principal("demo-central", "frontdesk")
    for p in (limited, desk):
        result = store.entities.resolve(p, "市区店")
        assert result["status"] == "resolved" and result["resolved_id"] == "demo-central"
        for word in ("江畔店", "江盼店", "demo-riverside"):
            assert store.entities.resolve(p, word)["status"] == "not_found"
    baseline = store.entities.resolve(limited, "中心店")
    store.entities.configure_aliases([*DEMO_ALIASES, {"hotel": "demo-riverside", "alias": "中心店"}])
    assert store.entities.resolve(limited, "中心店") == baseline
    assert_error("FORBIDDEN", store.entities.resolve, Principal("demo-central", "room", "101"), "中心店")


@pytest.mark.parametrize("query", ["火星度假村", "店", "demo-centra"])
def test_not_found_does_not_guess_or_fall_back_to_all_stores(sales, query):
    store, manager = sales
    result = store.entities.resolve(manager, query)
    assert result["status"] == "not_found" and result["candidates"] == []
    assert result["resolved_id"] is None and result["next_step"] == "ask_user"


@pytest.mark.parametrize("query", ["", "   ", "！？", "a" * 81, None, 123])
def test_resolver_rejects_invalid_name_input(sales, query):
    store, manager = sales
    assert_error("VALIDATION_ERROR", store.entities.resolve, manager, query)


def test_alias_config_replaces_atomically_and_empty_list_clears(sales):
    store, manager = sales
    aliases = [{"hotel": "demo-central", "alias": "ＣＥＮＴＲＡＬ"}]
    store.entities.configure_aliases(aliases)
    assert store.entities.resolve(manager, "central")["resolved_id"] == "demo-central"
    store.entities.configure_aliases(aliases)  # Reapplying local config is safe.
    baseline = store.entities.resolve(manager, "central")
    bad_configs = [None, [{"hotel": "demo-central", "alias": "!"}],
                   [{"hotel": "demo-central", "alias": "central", "kind": "guess"}],
                   [{"hotel": "demo-central", "alias": "central", "unexpected": 1}],
                   [*aliases, {"hotel": "demo-central", "alias": "central"}]]
    for bad in bad_configs:
        assert_error("VALIDATION_ERROR", store.entities.configure_aliases, bad)
        assert store.entities.resolve(manager, "central") == baseline
    assert_error("NOT_FOUND", store.entities.configure_aliases, [{"hotel": "unknown", "alias": "不存在"}])
    assert store.entities.resolve(manager, "central") == baseline
    with store.connect() as db:
        db.execute("CREATE TRIGGER inject_alias BEFORE INSERT ON store_aliases BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.entities.configure_aliases([{"hotel": "demo-central", "alias": "新名称"}])
    assert HotelStore(store.path).entities.resolve(manager, "central") == baseline
    with store.connect() as db:
        db.execute("DROP TRIGGER inject_alias")
    create_app({"principals": []}, store.path)  # Omission preserves persisted aliases.
    assert store.entities.resolve(manager, "central") == baseline
    create_app({"principals": [], "store_aliases": []}, store.path)
    assert store.entities.resolve(manager, "central")["status"] == "not_found"


def test_candidate_limit_does_not_turn_ambiguity_into_unique_result(sales):
    store, _ = sales
    hotels = tuple(f"branch-{i}" for i in range(7))
    with store.connect() as db:
        db.executemany("INSERT INTO stores VALUES(?,?)", [(h, f"测试门店{i}") for i, h in enumerate(hotels)])
    store.entities.configure_aliases([{"hotel": h, "alias": "连锁店"} for h in hotels])
    manager = Principal("", "manager", allowed_hotels=hotels)
    result = store.entities.resolve(manager, "连锁店")
    assert result["status"] == "needs_clarification" and result["resolved_id"] is None
    assert result["has_more"] and len(result["candidates"]) == 5
    assert "更完整" in result["clarification_question"]


@pytest.mark.parametrize("inject_failure", [False, True])
def test_v4_upgrade_preserves_sales_and_rolls_back_on_failure(sales, monkeypatch, inject_failure):
    from hotel_mcp import migrations
    store, manager = sales
    report = store.sales.query(manager, "2026-10-12", "2026-10-18")
    with store.connect() as db:
        remove_service_tables(db)
        db.execute("DROP TABLE store_aliases")
        db.execute("DELETE FROM schema_migrations WHERE version=5")
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_migrations'")]
        before = {t: [tuple(r) for r in db.execute(f'SELECT * FROM "{t}"')] for t in tables}
    with pytest.raises(ValueError, match="explicit --migrate"):
        HotelStore(store.path)
    if inject_failure:
        monkeypatch.setattr(migrations, "ENTITY_SCHEMA", migrations.ENTITY_SCHEMA + "INSERT INTO no_such_table VALUES(1);")
        with pytest.raises(sqlite3.OperationalError):
            HotelStore(store.path, migrate=True)
    else:
        HotelStore(store.path, migrate=True)
        HotelStore(store.path, migrate=True)
        assert store.sales.query(manager, "2026-10-12", "2026-10-18") == report
    with store.connect() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == (4 if inject_failure else 7)
        assert before == {t: [tuple(r) for r in db.execute(f'SELECT * FROM "{t}"')] for t in tables}
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if inject_failure:
            assert not db.execute("SELECT name FROM sqlite_master WHERE name='store_aliases'").fetchall()
        else:
            assert db.execute("SELECT COUNT(*) FROM store_aliases").fetchone()[0] == 0
