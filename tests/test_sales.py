"""Sales demo acceptance; exclusively pytest temporary directories/databases."""
import json
import sqlite3
import subprocess
import sys

import pytest

from hotel_mcp.demo import DEMO_STORES, init_sales_demo
from hotel_mcp.domain import Principal
from hotel_mcp.store import HotelStore
from tests.test_business import assert_error, business
from tests.schema_helpers import remove_service_tables


@pytest.fixture
def sales(tmp_path):
    directory = tmp_path / "demo"
    init_sales_demo(directory, "2026-10-18")
    store = HotelStore(directory / "hotel.sqlite3")
    manager = Principal("", "manager", principal_id="manager", allowed_hotels=tuple(h for h, _ in DEMO_STORES))
    return store, manager


def test_sales_summary_store_switch_categories_and_trend(sales):
    store, manager = sales
    listing = store.sales.list_stores(manager)
    assert "is_demo" not in listing and listing["data_source"] == "sales_ledger" and "演示" not in listing["display_notice"]
    assert len(listing["stores"]) == 3
    assert all(s["available_from"] == "2026-10-12" and s["available_to"] == "2026-10-18" for s in listing["stores"])
    report = store.sales.query(manager, "2026-10-12", "2026-10-18")
    assert report["totals"] == {"sales_amount_minor": 2452800, "order_count": 84}
    assert report["currency"] == "CNY" and "is_demo" not in report
    assert [r["hotel"] for r in report["by_store"]] == ["demo-airport", "demo-riverside", "demo-central"]
    for key in ("by_store", "by_category", "daily_trend"):
        for metric, total in report["totals"].items():
            assert sum(r[metric] for r in report[key]) == total
    day = store.sales.query(manager, "2026-10-18", "2026-10-18", ["demo-central"])
    assert day["totals"] == {"sales_amount_minor": 114000, "order_count": 4}
    assert len(day["daily_trend"]) == 1 and len(day["by_store"]) == 1
    room_sales = store.sales.query(manager, "2026-10-12", "2026-10-18", ["demo-central"], "accommodation")
    assert room_sales["totals"] == {"sales_amount_minor": 280700, "order_count": 7}
    assert len(room_sales["by_category"]) == 1
    # Reads do not create operation receipts/events or update any business state.
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 0
    assert HotelStore(store.path).sales.query(manager, "2026-10-12", "2026-10-18") == report


def test_sales_access_scope_and_no_manager_work_orders(sales):
    store, _ = sales
    desk = Principal("demo-central", "frontdesk")
    manager = Principal("", "manager", allowed_hotels=("demo-central",))
    room = Principal("demo-central", "room", "101")
    for p in (desk, manager):
        assert [r["hotel"] for r in store.sales.list_stores(p)["stores"]] == ["demo-central"]
        assert store.sales.query(p, "2026-10-12", "2026-10-18")["totals"]["order_count"] == 28
        for ids in (["demo-airport"], ["demo-central", "demo-airport"], ["unknown"], ["demo-central') OR 1=1 --"]):
            assert_error("FORBIDDEN", store.sales.query, p, "2026-10-12", "2026-10-18", ids)
    assert_error("FORBIDDEN", store.sales.query, room, "2026-10-12", "2026-10-18")
    assert_error("FORBIDDEN", store.sales.list_stores, room)
    assert_error("FORBIDDEN", store.list_requests, manager)
    assert_error("FORBIDDEN", store.start_stay, manager, "101", "2099-01-01", "key")
    assert_error("FORBIDDEN", store.inbox, manager)


@pytest.mark.parametrize("changes", [
    {"date_from": "2026-02-30"}, {"date_from": "20261012"}, {"date_to": "2026-10-11"},
    {"date_from": "2025-01-01"}, {"date_from": None}, {"hotel_ids": []},
    {"hotel_ids": ["demo-central", "demo-central"]}, {"hotel_ids": "demo-central"},
    {"hotel_ids": [False]}, {"category": "repair"},
])
def test_sales_input_validation(sales, changes):
    store, manager = sales
    assert_error("VALIDATION_ERROR", store.sales.query, manager,
                 **{"date_from": "2026-10-12", "date_to": "2026-10-18", **changes})


def test_empty_sales_zero_fill_and_missing_store(sales, tmp_path):
    store, manager = sales
    report = store.sales.query(manager, "2026-10-19", "2026-10-20")
    assert report["totals"] == {"sales_amount_minor": 0, "order_count": 0}
    assert len(report["by_store"]) == 3 and len(report["by_category"]) == 4 and len(report["daily_trend"]) == 2
    empty = HotelStore(tmp_path / "empty.db")
    assert empty.sales.list_stores(manager)["stores"] == []
    assert empty.sales.query(manager, "2026-10-12", "2026-10-18")["totals"]["order_count"] == 0
    assert_error("NOT_FOUND", empty.sales.query, manager, "2026-10-12", "2026-10-18", ["demo-central"])
    # The inclusive 366-day limit accepts leap years.
    assert len(store.sales.query(manager, "2024-01-01", "2024-12-31")["daily_trend"]) == 366


@pytest.mark.parametrize("kwargs", [
    {}, {"allowed_hotels": ()}, {"allowed_hotels": "demo-central"}, {"allowed_hotels": ("a", "a")},
    {"allowed_hotels": (" ",)}, {"allowed_hotels": (False,)}, {"allowed_hotels": ("a",), "room": "101"},
    {"allowed_hotels": ("a",), "hotel": "a"}, {"allowed_hotels": tuple(str(i) for i in range(101))},
])
def test_manager_requires_explicit_valid_scope(kwargs):
    with pytest.raises(ValueError):
        Principal(**{"hotel": "", "role": "manager", **kwargs})


def test_v3_upgrade_preserves_business_data_and_does_not_seed_sales(business):
    store, room, desk = business
    request = store.extensions.submit(room, "2099-01-02", "extension")["request"]
    store.extensions.quote(desk, request["id"], 39800, "人工核实报价", 1, "quote")
    tables = ("rooms", "stays", "requests", "request_actions", "stay_extension_details", "stay_extension_quotes",
              "operation_receipts", "events", "service_metadata", "sqlite_sequence")
    with store.connect() as db:
        remove_service_tables(db)
        db.execute("DROP TABLE store_aliases")
        db.execute("DROP TABLE demo_sales_orders")
        db.execute("DROP TABLE stores")
        db.execute("DELETE FROM schema_migrations WHERE version>=4")
        before = {name: [tuple(r) for r in db.execute(f"SELECT * FROM {name}")] for name in tables}
    with pytest.raises(ValueError, match="explicit --migrate"):
        HotelStore(store.path)
    migrated = HotelStore(store.path, migrate=True)
    HotelStore(store.path, migrate=True)
    with migrated.connect() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 7
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        after = {name: [tuple(r) for r in db.execute(f"SELECT * FROM {name}")] for name in tables}
        before["sqlite_sequence"].sort()
        after["sqlite_sequence"].sort()
        assert before == after
        assert db.execute("SELECT COUNT(*) FROM demo_sales_orders").fetchone()[0] == 0
    assert migrated.sales.query(desk, "2026-10-12", "2026-10-18")["totals"]["order_count"] == 0


def test_v4_migration_failure_rolls_back_all_changes(tmp_path, monkeypatch):
    from hotel_mcp import migrations
    path = tmp_path / "v3.db"
    store = HotelStore(path)
    with store.connect() as db:
        remove_service_tables(db)
        db.execute("DROP TABLE store_aliases")
        db.execute("DROP TABLE demo_sales_orders")
        db.execute("DROP TABLE stores")
        db.execute("DELETE FROM schema_migrations WHERE version>=4")
    monkeypatch.setattr(migrations, "SALES_SCHEMA", migrations.SALES_SCHEMA + "INSERT INTO no_such_table VALUES(1);")
    with pytest.raises(sqlite3.OperationalError):
        HotelStore(path, migrate=True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN ('stores','demo_sales_orders')").fetchall()


def test_demo_cli_isolated_generation_no_overwrite_or_token_output(tmp_path):
    directory = tmp_path / "demo"
    command = [sys.executable, "-m", "hotel_mcp", "--init-sales-demo", str(directory), "--demo-end-date", "2026-10-18"]
    run = subprocess.run(command, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["order_count"] == 84
    config = json.loads((directory / "config.json").read_text(encoding="utf-8"))
    tokens = [r["token"] for r in config["principals"]]
    assert len(set(tokens)) == len(tokens) and all(len(t) >= 32 and t not in run.stdout + run.stderr for t in tokens)
    before = {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    repeated = subprocess.run(command, capture_output=True, text=True)
    assert repeated.returncode != 0
    assert before == {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}
    refused = subprocess.run(command + ["--database", str(directory / "hotel.sqlite3")], capture_output=True, text=True)
    assert refused.returncode != 0
    invalid = subprocess.run([sys.executable, "-m", "hotel_mcp", "--init-sales-demo", str(tmp_path / "bad"),
                              "--demo-end-date", "bad-date"], capture_output=True, text=True)
    assert invalid.returncode != 0 and not (tmp_path / "bad").exists()


def test_public_presentation_preserves_internal_provenance(sales):
    store, manager = sales
    with store.connect() as db:
        before = [tuple(r) for r in db.execute("SELECT * FROM demo_sales_orders ORDER BY id")]
        names = [tuple(r) for r in db.execute("SELECT hotel,name FROM stores ORDER BY hotel")]
    listing = store.sales.list_stores(manager)
    report = store.sales.query(manager, "2026-10-12", "2026-10-18")
    for value in (listing, report):
        assert "is_demo" not in value and value["data_source"] == "sales_ledger"
        assert "演示" not in json.dumps(value, ensure_ascii=False)
    ambiguity = store.entities.resolve(manager, "市区店")
    assert "编号" not in ambiguity["clarification_question"]
    assert "demo-" not in ambiguity["clarification_question"]
    assert "演示" not in ambiguity["clarification_question"]
    assert {c["entity_id"] for c in ambiguity["candidates"]} == {"demo-central", "demo-riverside"}
    with store.connect() as db:
        assert [tuple(r) for r in db.execute("SELECT * FROM demo_sales_orders ORDER BY id")] == before
        assert [tuple(r) for r in db.execute("SELECT hotel,name FROM stores ORDER BY hotel")] == names
