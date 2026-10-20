"""Temporary-database acceptance for manually handled stay extensions."""
import hashlib
import base64
import hmac
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hotel_mcp import migrations, stay_extensions
from hotel_mcp.domain import BusinessError, Principal, canonical
from hotel_mcp.store import HotelStore
from tests.test_business import assert_error, business, repair


def submit(store, room, key="extension", target="2099-01-03", note=""):
    return store.extensions.submit(room, target, key, note)["request"]


def quote(store, desk, order, key="quote", **kwargs):
    return store.extensions.quote(desk, order["id"], 76000, "两晚含早餐，前台办理", order["version"], key, **kwargs)["request"]


def confirm(store, room, order, key="confirm"):
    return store.extensions.confirm(room, order["id"], order["extension"]["current_quote_id"], order["version"], key)["request"]


def begin(store, desk, order, key="begin"):
    return store.extensions.begin(desk, order["id"], order["extension"]["current_quote_id"], "已核实房态，开始办理", order["version"], key)["request"]


def finish(store, desk, order, outcome="success", key="finish"):
    return store.extensions.finish(desk, order["id"], order["extension"]["current_quote_id"], outcome,
                                   "人工办理结果已核实", order["version"], key, "manual-001")


def test_success_only_updates_stay_at_finish_and_all_steps_replay(business):
    store, room, desk = business
    ext = store.extensions
    initial = store.current_stay(room)["stay"]
    order = submit(store, room)
    assert ext.submit(room, "2099-01-03", "extension")["replayed"]
    offered = quote(store, desk, order)
    q = offered["extension"]["current_quote"]
    assert q["nights"] == 2 and q["total_amount_minor"] == 76000 and q["currency"] == "CNY"
    assert q["base_stay_version"] == initial["version"]
    confirmed = confirm(store, room, offered)
    processing = begin(store, desk, confirmed)
    assert store.current_stay(room)["stay"] == initial
    completed = finish(store, desk, processing)
    assert completed["request"]["status"] == "completed"
    assert completed["stay"]["id"] == room.stay_id
    assert completed["stay"]["expected_checkout_date"] == "2099-01-03"
    assert completed["stay"]["version"] == initial["version"] + 1
    assert finish(store, desk, processing)["replayed"]
    assert ext.quote(desk, order["id"], 76000, "两晚含早餐，前台办理", 1, "quote")["replayed"]
    assert ext.confirm(room, order["id"], q["id"], 2, "confirm")["replayed"]
    assert ext.begin(desk, order["id"], q["id"], "已核实房态，开始办理", 3, "begin")["replayed"]
    assert ext.submit(room, "2099-01-03", "extension")["request"]["status"] == "pending"  # original result
    assert store.current_stay(room)["stay"]["version"] == 2
    history = store.list_actions(desk, order["id"])["actions"]
    assert [a["action"] for a in history] == ["finish_success", "begin", "confirm", "quote", "submit"]
    assert history[0]["data"]["result_reference"] == "manual-001"
    assert history[2]["data"]["quote_id"] == q["id"]
    assert [e["event_type"] for e in store.inbox(desk)["events"]] == ["hotel.stay_extension.submitted", "hotel.stay_extension.confirmed"]
    guest_events = store.inbox(room)["events"]
    assert [e["event_type"] for e in guest_events] == ["hotel.stay_extension.quoted", "hotel.stay_extension.processing", "hotel.stay_extension.completed"]
    assert guest_events[-1]["quote_id"] == q["id"] and guest_events[-1]["target_checkout_date"] == "2099-01-03"
    # A later extension starts from the successfully extended date.
    another = submit(store, room, "another", "2099-01-05")
    assert another["extension"]["original_checkout_date"] == "2099-01-03"


def test_failed_and_unknown_results_do_not_extend_stay(business):
    store, room, desk = business
    initial = store.current_stay(room)["stay"]
    processing = begin(store, desk, confirm(store, room, quote(store, desk, submit(store, room))))
    noted = store.process_request(desk, processing["id"], "add_note", "结果尚不确定，继续核实", 4, "unknown")["request"]
    assert noted["status"] == "processing" and store.current_stay(room)["stay"] == initial
    result = finish(store, desk, noted, "failed")
    assert result["request"]["status"] == "failed" and result["stay"] == initial
    assert result["request"]["extension"]["result_at"]
    assert finish(store, desk, noted, "failed")["replayed"]
    assert_error("IDEMPOTENCY_CONFLICT", finish, store, desk, noted, "success")
    assert_error("INVALID_TRANSITION", store.process_request, desk, processing["id"], "add_note", "不能重开", 6, "reopen")
    assert submit(store, room, "retry")["status"] == "pending"


def test_quote_revision_invalidates_confirmation_without_losing_history(business):
    store, room, desk = business
    first = quote(store, desk, submit(store, room))
    old_quote = first["extension"]["current_quote"]
    confirmed = confirm(store, room, first)
    assert_error("VALIDATION_ERROR", quote, store, desk, confirmed, "missing-reason")
    revised = quote(store, desk, confirmed, "revision", change_reason="调整早餐服务条件")
    assert revised["status"] == "quoted" and revised["extension"]["confirmed_quote_id"] is None
    assert revised["extension"]["confirmed_at"] is None
    assert_error("QUOTE_SUPERSEDED", store.extensions.confirm, room, revised["id"], old_quote["id"], 4, "old-quote")
    assert_error("INVALID_TRANSITION", store.extensions.begin, desk, revised["id"], old_quote["id"], "办理", 4, "premature")
    current = confirm(store, room, revised, "confirm-new")
    assert current["extension"]["confirmed_quote_id"] != old_quote["id"]
    page = store.extensions.list_quotes(desk, current["id"], limit=1)
    older = store.extensions.list_quotes(desk, current["id"], limit=1, cursor=page["next_cursor"])
    assert [page["quotes"][0]["revision"], older["quotes"][0]["revision"]] == [2, 1]
    assert older["quotes"][0]["id"] == old_quote["id"]
    assert older["quotes"][0]["pricing_note"] == old_quote["pricing_note"]
    assert "actor_principal_id" not in store.extensions.list_quotes(room, current["id"])["quotes"][0]
    history = store.list_actions(desk, current["id"])["actions"]
    assert len([a for a in history if a["action"] == "confirm"]) == 2
    assert_error("VALIDATION_ERROR", store.extensions.list_quotes, room, current["id"], cursor=page["next_cursor"])


def test_quote_expiry_boundary_and_confirmed_quote_survives_expiry(business, monkeypatch):
    store, room, desk = business
    monkeypatch.setattr(stay_extensions, "utcnow", lambda: "2026-10-18T00:00:00.000000Z")
    offered = quote(store, desk, submit(store, room), valid_for_minutes=1)
    assert offered["extension"]["current_quote"]["expires_at"] == "2026-10-18T00:01:00.000000Z"
    monkeypatch.setattr(stay_extensions, "utcnow", lambda: "2026-10-18T00:01:00.000000Z")
    assert_error("QUOTE_EXPIRED", confirm, store, room, offered)
    assert store.get_request(room, offered["id"])["request"]["extension"]["quote_status"] == "expired"
    assert "confirm" not in store.get_request(room, offered["id"])["request"]["extension"]["available_actions"]["room"]
    renewed = quote(store, desk, offered, "renew", change_reason="过期重新核实")
    confirmed = confirm(store, room, renewed)
    monkeypatch.setattr(stay_extensions, "utcnow", lambda: "2026-10-19T00:00:00.000000Z")
    processing = begin(store, desk, confirmed)
    assert processing["extension"]["quote_status"] == "confirmed"
    assert finish(store, desk, processing)["request"]["status"] == "completed"


@pytest.mark.parametrize("target", ["2099-01-01", "2098-12-31", "2099-02-29", "not-a-date"])
def test_bad_dates(business, target):
    store, room, _ = business
    assert_error("VALIDATION_ERROR", submit, store, room, target=target)


@pytest.mark.parametrize("amount", [-1, 7.6, True, "76000", 9_007_199_254_740_992])
def test_bad_quote_amounts(business, amount):
    store, room, desk = business
    order = submit(store, room)
    assert_error("VALIDATION_ERROR", store.extensions.quote, desk, order["id"], amount, "说明", 1, "quote")


def test_free_quote_and_invalid_duration_and_notes(business):
    store, room, desk = business
    order = submit(store, room)
    for duration in (0, 1441, True, 1.5):
        assert_error("VALIDATION_ERROR", store.extensions.quote, desk, order["id"], 100, "说明", 1, "quote", valid_for_minutes=duration)
    assert_error("VALIDATION_ERROR", store.extensions.quote, desk, order["id"], 0, " ", 1, "quote")
    offered = store.extensions.quote(desk, order["id"], 0, "酒店补偿，免费续住两晚", 1, "quote")["request"]
    assert offered["extension"]["current_quote"]["total_amount_minor"] == 0


@pytest.mark.parametrize("stage", ["pending", "quoted", "confirmed", "processing"])
def test_cancel_reject_and_checkout_rules(business, stage):
    store, room, desk = business
    repair_order = repair(store, room)
    order = submit(store, room)
    if stage != "pending":
        order = quote(store, desk, order)
    if stage in ("confirmed", "processing"):
        order = confirm(store, room, order)
    if stage == "processing":
        order = begin(store, desk, order)
    assert_error("ACTIVE_EXTENSION_EXISTS", submit, store, room, "duplicate")
    if stage in ("confirmed", "processing"):
        assert_error("INVALID_TRANSITION", store.cancel_request, room, order["id"], "取消", order["version"], "guest-cancel")
        assert_error("INVALID_TRANSITION", store.extensions.reject, desk, order["id"], "拒绝", order["version"], "reject")
    if stage == "processing":
        assert_error("INVALID_TRANSITION", store.cancel_request, desk, order["id"], "取消", 4, "desk-cancel")
        assert_error("INVALID_TRANSITION", quote, store, desk, order, "requote", change_reason="改价")
        assert_error("STAY_HAS_PROCESSING_EXTENSION", store.end_stay, desk, room.stay_id, 1, "退房", "checkout")
        assert store.current_stay(room)["stay"]["status"] == "active"
    else:
        ended = store.end_stay(desk, room.stay_id, 1, "退房", "checkout")
        assert ended["cancelled_extension_count"] == 1 and ended["open_request_count"] == 1
        assert store.get_request(desk, order["id"])["request"]["status"] == "cancelled"
        assert store.get_request(desk, repair_order["id"])["request"]["status"] == "pending"
        assert store.end_stay(desk, room.stay_id, 1, "退房", "checkout")["replayed"]
        assert store.inbox(desk)["events"][-1]["event_type"] == "hotel.stay_extension.cancelled"
        assert_error("STAY_INACTIVE", store.extensions.list_quotes, room, order["id"])
        new_stay = store.start_stay(desk, "101", "2099-01-01", "new-stay")["stay"]
        next_room = Principal("a", "room", "101", new_stay["id"], "room")
        assert store.inbox(next_room)["events"] == []
        assert_error("NOT_FOUND", store.extensions.list_quotes, next_room, order["id"])
        assert submit(store, next_room)["id"] != order["id"]


@pytest.mark.parametrize("action", ["cancel_pending", "cancel_quoted", "desk_cancel_confirmed", "reject_pending", "reject_quoted"])
def test_manual_exception_exits(business, action):
    store, room, desk = business
    order = submit(store, room)
    if "quoted" in action or "confirmed" in action:
        order = quote(store, desk, order)
    if "confirmed" in action:
        order = confirm(store, room, order)
    if action.startswith("reject"):
        result = store.extensions.reject(desk, order["id"], "房态不允许", order["version"], "end")
        assert result["request"]["status"] == "rejected"
    else:
        result = store.cancel_request(desk if action.startswith("desk") else room, order["id"], "不再需要", order["version"], "end")
        assert result["request"]["status"] == "cancelled"
    assert store.current_stay(room)["stay"]["expected_checkout_date"] == "2099-01-01"
    assert submit(store, room, "new")["status"] == "pending"


def test_guards_scopes_and_stay_snapshot(business):
    store, room, desk = business
    order = submit(store, room)
    other = Principal("b", "frontdesk", principal_id="other")
    assert_error("NOT_FOUND", quote, store, other, order)
    assert_error("FORBIDDEN", quote, store, room, order)
    assert_error("IDEMPOTENCY_CONFLICT", submit, store, room, target="2099-01-04")
    assert_error("IDEMPOTENCY_CONFLICT", store.submit, room, "extension", "其他请求")
    for action in ("accept", "complete", "reject", "close_unresolved"):
        assert_error("INVALID_TRANSITION", store.process_request, desk, order["id"], action, "说明", 1, action)
    assert_error("VALIDATION_ERROR", store.transition, desk, order["id"], "completed")
    offered = quote(store, desk, order)
    qid = offered["extension"]["current_quote_id"]
    assert_error("FORBIDDEN", store.extensions.confirm, desk, order["id"], qid, 2, "wrong-actor")
    assert_error("INVALID_TRANSITION", store.extensions.finish, desk, order["id"], qid, "success", "不能跳过办理", 2, "early")
    with store.connect() as db:
        db.execute("UPDATE stays SET version=version+1 WHERE id=?", (room.stay_id,))
    assert_error("STAY_CHANGED", confirm, store, room, offered)
    renewed = quote(store, desk, offered, "rechecked", change_reason="重新核对入住版本")
    confirmed = confirm(store, room, renewed)
    with store.connect() as db:
        db.execute("UPDATE stays SET expected_checkout_date='2099-01-02',version=version+1 WHERE id=?", (room.stay_id,))
    assert_error("STAY_CHANGED", begin, store, desk, confirmed)


@pytest.mark.parametrize("table", ["stays", "request_actions", "events", "operation_receipts"])
def test_finish_failure_rolls_back_date_and_all_artifacts(business, table):
    store, room, desk = business
    processing = begin(store, desk, confirm(store, room, quote(store, desk, submit(store, room))))
    initial_stay = store.current_stay(room)["stay"]
    initial_history = store.list_actions(desk, processing["id"])
    initial_events = store.inbox(room)
    with store.connect() as db:
        kind = "UPDATE" if table == "stays" else "INSERT"
        db.execute(f"CREATE TRIGGER inject BEFORE {kind} ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        finish(store, desk, processing)
    assert store.current_stay(room)["stay"] == initial_stay
    assert store.get_request(desk, processing["id"])["request"]["status"] == "processing"
    assert store.get_request(desk, processing["id"])["request"]["extension"]["result_at"] is None
    assert store.list_actions(desk, processing["id"]) == initial_history
    assert store.inbox(room) == initial_events
    with store.connect() as db:
        db.execute("DROP TRIGGER inject")
    assert finish(store, desk, processing)["request"]["status"] == "completed"


def test_checkout_cancellation_is_atomic(business):
    store, room, desk = business
    order = quote(store, desk, submit(store, room))
    with store.connect() as db:
        db.execute("CREATE TRIGGER inject BEFORE UPDATE ON stays BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.end_stay(desk, room.stay_id, 1, "退房", "end")
    assert store.current_stay(room)["stay"]["status"] == "active"
    assert store.get_request(room, order["id"])["request"]["status"] == "quoted"
    assert len(store.list_actions(desk, order["id"])["actions"]) == 2


def test_confirm_requote_and_begin_cancel_races(business):
    store, room, desk = business
    offered = quote(store, desk, submit(store, room))
    barrier = Barrier(2)
    def race(fn):
        barrier.wait()
        try:
            return fn()
        except BusinessError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, [lambda: confirm(store, room, offered),
                                      lambda: quote(store, desk, offered, "revised", change_reason="调整报价")]))
    assert sum(isinstance(r, dict) for r in results) == 1 and "VERSION_CONFLICT" in results
    current = store.get_request(room, offered["id"])["request"]
    if current["status"] == "quoted":
        current = confirm(store, room, current, "new-confirm")
    barrier = Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(race, [lambda: begin(store, desk, current),
                                      lambda: store.cancel_request(desk, current["id"], "取消", current["version"], "cancel")]))
    assert sum(isinstance(r, dict) for r in results) == 1 and "VERSION_CONFLICT" in results


def v2_database(path, corrupt=False, empty_requests=False):
    """Build the exact frozen P0 schema without using the current initializer."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    with db:
        db.executescript(migrations.SCHEMA)
        db.execute("INSERT INTO schema_migrations(version) VALUES(2)")
        db.execute("INSERT INTO service_metadata VALUES('cursor_secret',?)", ("ab" * 32,))
        db.execute("INSERT INTO rooms VALUES('a','101',1)")
        db.execute("INSERT INTO stays(id,hotel,room,status,check_in_at,expected_checkout_date) VALUES('stay','a','101','active','2026-10-18T00:00:00.000000Z','2099-01-01')")
        for seq, request_id, kind in ((3, "general", "general"), (8, "repair", "repair")):
            db.execute("INSERT INTO requests(seq,id,hotel,room,stay_id,request_key,request_type,description,created_at,updated_at) "
                       "VALUES(?,?,'a','101','stay',?,?,'原请求','2026-10-18T00:00:00.000000Z','2026-10-18T00:00:00.000000Z')", (seq, request_id, request_id, kind))
        db.execute("INSERT INTO repair_details VALUES('repair','other','','normal',NULL)")
        db.execute("INSERT INTO request_actions VALUES(12,?,'submit',NULL,'pending','原请求','room','room','2026-10-18T00:00:00.000000Z',1)", ("absent" if corrupt else "general",))
        db.execute("INSERT INTO events VALUES(22,'a','frontdesk','',?,'stay')", (json.dumps({"event_type": "hotel.request.pending", "speech_text": "旧事件"}),))
        row = db.execute("SELECT * FROM requests WHERE id='general'").fetchone()
        response = {"request": {**HotelStore.public(row), "stay_id": "stay", "request_type": "general", "version": 1}, "replayed": False}
        digest = hashlib.sha256(canonical(["原请求", None]).encode()).hexdigest()
        db.execute("INSERT INTO operation_receipts VALUES('a','room','stay','submit_general','general',?,?,?)", (digest, canonical(response), "2026-10-18T00:00:00.000000Z"))
        db.execute("UPDATE sqlite_sequence SET seq=100 WHERE name='requests'")
        db.execute("UPDATE sqlite_sequence SET seq=200 WHERE name='events'")
        if empty_requests:
            db.execute("DELETE FROM repair_details")
            db.execute("DELETE FROM request_actions")
            db.execute("DELETE FROM requests")
    db.close()


@pytest.mark.parametrize("empty", [False, True])
def test_v2_upgrade_preserves_relationships_sequences_receipts_and_cursor_secret(tmp_path, empty):
    path = tmp_path / "v2.db"
    v2_database(path, empty_requests=empty)
    with pytest.raises(ValueError, match="explicit --migrate"):
        HotelStore(path)
    store = HotelStore(path, migrate=True)
    assert store.cursor_secret.hex() == "ab" * 32
    desk, room = Principal("a", "frontdesk"), Principal("a", "room", "101", "stay", "room")
    if not empty:
        # A cursor issued by v2 must still paginate the same sequence after rebuild.
        context = hashlib.sha256(canonical(["a", "frontdesk", "", "", desk.actor_id, "requests", [None] * 6]).encode()).hexdigest()
        body = canonical({"context": context, "upper": 8, "last": 8}).encode()
        old_cursor = base64.urlsafe_b64encode(hmac.digest(bytes.fromhex("ab" * 32), body, "sha256") + body).decode()
        assert [r["id"] for r in store.query_requests(desk, cursor=old_cursor)["requests"]] == ["general"]
        assert store.submit(room, "general", "原请求")["id"] == "general"
        assert store.get_request(room, "repair")["request"]["repair"]["category"] == "other"
        assert store.list_actions(desk, "general")["actions"][0]["data"] == {}
    order = submit(store, room)
    assert store.inbox(desk)["events"][-1]["seq"] > 200
    with store.connect() as db:
        assert db.execute("SELECT seq FROM requests WHERE id=?", (order["id"],)).fetchone()[0] > 100
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 6
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    HotelStore(path, migrate=True)


def test_v2_bad_foreign_key_upgrade_rolls_back_and_reenables_checks(tmp_path):
    path = tmp_path / "broken-v2.db"
    v2_database(path, corrupt=True)
    with pytest.raises(sqlite3.IntegrityError, match="foreign key check"):
        HotelStore(path, migrate=True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        assert "data" not in [r[1] for r in db.execute("PRAGMA table_info(request_actions)")]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='stay_extension_details'").fetchone()
        assert db.execute("SELECT seq FROM sqlite_sequence WHERE name='requests'").fetchone()[0] == 100


@pytest.mark.parametrize("step", ["quote", "confirm", "begin"])
def test_intermediate_failure_rolls_back_details_history_and_receipts(business, step):
    store, room, desk = business
    order = submit(store, room)
    if step in ("confirm", "begin"):
        order = quote(store, desk, order)
    if step == "begin":
        order = confirm(store, room, order)
    before = store.get_request(desk, order["id"])
    quotes_before = store.extensions.list_quotes(desk, order["id"])
    history_before = store.list_actions(desk, order["id"])
    with store.connect() as db:
        db.execute("CREATE TRIGGER inject BEFORE INSERT ON operation_receipts BEGIN SELECT RAISE(ABORT,'injected'); END")
    operation = {"quote": lambda: quote(store, desk, order), "confirm": lambda: confirm(store, room, order),
                 "begin": lambda: begin(store, desk, order)}[step]
    with pytest.raises(sqlite3.IntegrityError):
        operation()
    assert store.get_request(desk, order["id"]) == before
    assert store.extensions.list_quotes(desk, order["id"]) == quotes_before
    assert store.list_actions(desk, order["id"]) == history_before
    with store.connect() as db:
        db.execute("DROP TRIGGER inject")
    assert operation()["version"] == order["version"] + 1


def test_concurrent_distinct_applications_and_database_uniqueness(business):
    store, room, desk = business
    barrier = Barrier(2)
    def apply(key):
        barrier.wait()
        try:
            return submit(store, room, key)
        except BusinessError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, ["one", "two"]))
    assert sum(isinstance(r, dict) for r in results) == 1 and "ACTIVE_EXTENSION_EXISTS" in results
    assert len(store.query_requests(desk, request_type="stay_extension")["requests"]) == 1
    with store.connect() as db:
        # The rule is also enforced by SQLite if a future writer misses the pre-check.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("INSERT INTO requests(id,hotel,room,stay_id,request_key,request_type,description,created_at,updated_at) "
                       "VALUES('bypass','a','101',?,'different','stay_extension','duplicate','now','now')", (room.stay_id,))
