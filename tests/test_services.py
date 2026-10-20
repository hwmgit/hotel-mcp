import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hotel_mcp.demo import init_full_demo
from hotel_mcp.domain import BusinessError, Principal
from hotel_mcp.store import HotelStore
from tests.test_business import assert_error
from tests.test_stay_extensions import v2_database


@pytest.fixture
def services(tmp_path):
    root = tmp_path / "full-demo"
    summary = init_full_demo(root, "2026-10-18")
    config = json.loads((root / "config.json").read_text(encoding="utf-8"))
    store = HotelStore(root / "hotel.sqlite3")
    people = {r["id"]: Principal(r.get("hotel", ""), r["role"], r.get("room", ""), r.get("stay_id", ""), r["id"],
                                 tuple(r.get("allowed_hotels", []))) for r in config["principals"]}
    return store, people["room-demo-central-101"], people["frontdesk-demo-central"], summary, config


def apply(store, desk, row, action, key=None):
    return store.process_request(desk, row["id"], action, "前台已核实", row["version"], key or f"{row['id']}-{action}")["request"]


def test_meal_price_snapshot_complete_idempotency_and_sales_separation(services):
    store, room, desk, _, _ = services
    before = store.sales.query(desk, "2026-10-12", "2026-10-18")
    item = store.services.menu(room)["items"][0]
    meal = store.services.submit_meal(room, [{"item_id": item["id"], "quantity": 2}], "meal-new", "少盐")["request"]
    assert meal["meal"]["total_amount_minor"] == item["price_minor"] * 2
    assert meal["meal"]["settlement"] == "offline"
    with store.connect() as db:
        db.execute("UPDATE menu_items SET price_minor=1,name='新菜单名称',enabled=0 WHERE id=?", (item["id"],))
    replay = store.services.submit_meal(room, [{"item_id": item["id"], "quantity": 2}], "meal-new", "少盐")
    assert replay["replayed"] and replay["request"] == meal
    accepted = apply(store, desk, meal, "accept")
    assert apply(store, desk, meal, "accept") == accepted  # Same key replays original response.
    completed = apply(store, desk, accepted, "complete")
    assert completed["status"] == "completed" and completed["meal"] == meal["meal"]
    assert [a["action"] for a in store.list_actions(room, meal["id"])["actions"]] == ["complete", "accept", "submit"]
    assert store.inbox(room)["events"][-1]["event_type"] == "hotel.meal.completed"
    assert store.sales.query(desk, "2026-10-12", "2026-10-18") == before
    assert_error("ITEM_UNAVAILABLE", store.services.submit_meal, room, [{"item_id": item["id"], "quantity": 1}], "new-key")
    assert_error("VALIDATION_ERROR", store.transition, desk, meal["id"], "accepted")


@pytest.mark.parametrize("items", [[], [{"item_id": "x", "quantity": 0}], [{"item_id": "x", "quantity": True}],
                                  [{"item_id": "x", "quantity": 1.5}], [{"item_id": "x", "quantity": 21}],
                                  [{"item_id": "x", "quantity": 1}, {"item_id": "x", "quantity": 2}]])
def test_meal_validation(services, items):
    store, room, _, _, _ = services
    assert_error("VALIDATION_ERROR", store.services.submit_meal, room, items, "bad")


@pytest.mark.parametrize("kind,participants", [("gym", 3), ("meeting", 6)])
def test_booking_confirmation_capacity_cancellation_and_completion(services, kind, participants):
    store, room, desk, summary, _ = services
    slot = store.services.slots(room, kind, summary["booking_date"])["slots"][1]
    a = store.services.submit_booking(room, slot["id"], participants, "a")["request"]
    b = store.services.submit_booking(room, slot["id"], participants, "b")["request"]
    assert store.services.slots(desk, kind, summary["booking_date"])["slots"][1]["remaining"] == slot["capacity"]
    accepted = apply(store, desk, a, "accept")
    assert_error("RESOURCE_UNAVAILABLE", apply, store, desk, b, "accept")
    assert store.get_request(room, b["id"])["request"]["status"] == "pending"
    assert_error("INVALID_TRANSITION", store.cancel_request, room, a["id"], "取消", accepted["version"], "room-cancel")
    store.cancel_request(desk, a["id"], "前台确认取消", accepted["version"], "desk-cancel")
    accepted_b = apply(store, desk, b, "accept")  # Failed confirmation did not consume request_key.
    completed = apply(store, desk, accepted_b, "complete")
    assert completed["status"] == "completed"
    assert store.services.slots(desk, kind, summary["booking_date"])["slots"][1]["remaining"] == slot["capacity"]
    assert store.inbox(room)["events"][-1]["event_type"] == f"hotel.{'gym_booking' if kind=='gym' else 'meeting_booking'}.completed"


def test_concurrent_booking_confirmation_never_overbooks(services):
    store, room, desk, summary, _ = services
    slot = store.services.slots(room, "meeting", summary["booking_date"])["slots"][0]
    orders = [store.services.submit_booking(room, slot["id"], 2, str(i))["request"] for i in range(2)]
    barrier = Barrier(2)
    def confirm(order):
        barrier.wait()
        try:
            return apply(store, desk, order, "accept")
        except BusinessError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, orders))
    assert sum(isinstance(r, dict) for r in results) == 1 and "RESOURCE_UNAVAILABLE" in results
    assert store.services.slots(desk, "meeting", summary["booking_date"])["slots"][0]["remaining"] == 0


def test_service_authorization_conflicts_and_expired_or_disabled_slots(services, monkeypatch):
    store, room, desk, summary, config = services
    slot = store.services.slots(room, "gym", summary["booking_date"])["slots"][0]
    other = Principal("demo-airport", "frontdesk")
    assert_error("FORBIDDEN", store.services.submit_booking, desk, slot["id"], 1, "x")
    assert_error("FORBIDDEN", store.services.menu, Principal("", "manager", allowed_hotels=("demo-central",)))
    assert_error("ITEM_UNAVAILABLE", store.services.submit_meal, room, [{"item_id": "demo-airport-noodles", "quantity": 1}], "other")
    assert_error("NOT_FOUND", store.services.submit_booking, room, slot["id"].replace("central", "airport"), 1, "other")
    assert_error("VALIDATION_ERROR", store.services.submit_booking, room, slot["id"], 5, "too-many")
    assert_error("VALIDATION_ERROR", store.services.submit_booking, room, slot["id"], True, "bool")
    booking = store.services.submit_booking(room, slot["id"], 1, "one")["request"]
    assert_error("NOT_FOUND", store.get_request, other, booking["id"])
    assert_error("IDEMPOTENCY_CONFLICT", store.services.submit_booking, room, slot["id"], 2, "one")
    assert_error("IDEMPOTENCY_CONFLICT", store.services.submit_meal, room, [{"item_id": "demo-central-noodles", "quantity": 1}], "one")
    with store.connect() as db:
        db.execute("UPDATE booking_resources SET enabled=0 WHERE id=?", (slot["resource_id"],))
    assert_error("NOT_FOUND", apply, store, desk, booking, "accept")
    with store.connect() as db:
        db.execute("UPDATE booking_resources SET enabled=1 WHERE id=?", (slot["resource_id"],))
    monkeypatch.setattr("hotel_mcp.services.utcnow", lambda: slot["starts_at"])
    assert_error("SLOT_EXPIRED", apply, store, desk, booking, "accept")


def test_checkout_cancels_bookings_releases_capacity_and_keeps_accepted_meals(services):
    store, room, desk, summary, _ = services
    meals = store.query_requests(room, request_type="meal")["requests"]
    accepted_meal = apply(store, desk, meals[0], "accept")
    gym = store.query_requests(room, request_type="gym_booking")["requests"][0]
    apply(store, desk, gym, "accept")
    stay = store.current_stay(room)["stay"]
    ended = store.end_stay(desk, stay["id"], stay["version"], "退房", "checkout")
    assert ended["cancelled_service_count"] == 1
    assert store.get_request(desk, gym["id"])["request"]["status"] == "cancelled"
    assert store.get_request(desk, accepted_meal["id"])["request"]["status"] == "accepted"
    assert store.services.slots(desk, "gym", summary["booking_date"])["slots"][0]["remaining"] == 4
    assert_error("STAY_INACTIVE", store.services.menu, room)


def test_failed_receipt_rolls_back_reservation_occupancy_history_events(services):
    store, room, desk, summary, _ = services
    row = store.query_requests(room, request_type="gym_booking")["requests"][0]
    before = store.get_request(desk, row["id"])
    history = store.list_actions(desk, row["id"])
    inbox = store.inbox(room)
    with store.connect() as db:
        db.execute("CREATE TRIGGER injected BEFORE INSERT ON operation_receipts BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        apply(store, desk, row, "accept")
    assert store.get_request(desk, row["id"]) == before
    assert store.list_actions(desk, row["id"]) == history and store.inbox(room) == inbox
    assert store.services.slots(desk, "gym", summary["booking_date"])["slots"][0]["remaining"] == 4


def test_slot_overlap_prevented_but_adjacent_slots_allowed(services):
    store, _, _, summary, _ = services
    with store.connect() as db:
        row = db.execute("SELECT * FROM booking_slots LIMIT 1").fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="overlapping"):
            db.execute("INSERT INTO booking_slots VALUES(?,?,?,?,?,?)",
                       ("duplicate-time", row["hotel"], row["resource_id"], row["business_date"], row["starts_at"], row["ends_at"]))
        end = row["ends_at"][:11] + "03:00:00.000000Z"  # original slot is 01:00–02:00 UTC
        db.execute("INSERT INTO booking_slots VALUES(?,?,?,?,?,?)",
                   ("adjacent", row["hotel"], row["resource_id"], row["business_date"], row["ends_at"], end))


@pytest.mark.parametrize("broken", [False, True])
def test_frozen_v5_migration_data_integrity_highwater_and_rollback(tmp_path, monkeypatch, broken):
    from hotel_mcp import migrations as m
    path = tmp_path / "v5.db"
    v2_database(path)
    with sqlite3.connect(path) as db:
        m._upgrade_v3(db)
        m._execute_schema(db, m.SALES_SCHEMA)
        m._execute_schema(db, m.ENTITY_SCHEMA)
        db.executemany("INSERT INTO schema_migrations(version) VALUES(?)", [(4,), (5,)])
        db.execute("INSERT INTO stores VALUES('a','原门店')")
        db.execute("INSERT INTO store_aliases VALUES('a','别名','别名','alias')")
        db.execute("INSERT INTO demo_sales_orders VALUES('sale','a','2026-10-18','dining',1234,'CNY')")
        db.execute("INSERT INTO requests(id,hotel,room,stay_id,request_key,request_type,description,created_at,updated_at) "
                   "VALUES('extension','a','101','stay','extension','stay_extension','续房','now','now')")
        db.execute("INSERT INTO stay_extension_details(request_id,original_checkout_date,requested_checkout_date) VALUES('extension','2099-01-01','2099-01-02')")
        db.execute("UPDATE sqlite_sequence SET seq=500 WHERE name='requests'")
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_migrations'")]
        before = {t: db.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}
    with pytest.raises(ValueError, match="explicit --migrate"):
        HotelStore(path)
    if broken:
        monkeypatch.setattr(m, "SERVICE_SCHEMA", m.SERVICE_SCHEMA + "INSERT INTO no_table VALUES(1);")
        with pytest.raises(sqlite3.OperationalError):
            HotelStore(path, migrate=True)
    else:
        HotelStore(path, migrate=True)
        HotelStore(path, migrate=True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == (5 if broken else 6)
        assert before == {t: db.execute(f'SELECT * FROM "{t}"').fetchall() for t in tables}
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        if broken:
            assert not db.execute("SELECT name FROM sqlite_master WHERE name='menu_items'").fetchone()
