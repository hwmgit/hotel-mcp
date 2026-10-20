"""Business acceptance tests: all databases are pytest temporary files."""
import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from hotel_mcp.domain import BusinessError
from hotel_mcp.store import HotelStore, Principal


@pytest.fixture
def business(tmp_path):
    store = HotelStore(tmp_path / "hotel.db")
    desk = Principal("a", "frontdesk", principal_id="desk")
    store.configure_rooms([{"hotel": "a", "room": "101"}, {"hotel": "a", "room": "102"}])
    stay = store.start_stay(desk, "101", "2099-01-01", "checkin")["stay"]
    room = Principal("a", "room", "101", stay["id"], "room")
    return store, room, desk


def repair(store, room, key="repair-1", **kwargs):
    return store.submit_repair(room, key, "air_conditioning", "空调不制冷", **kwargs)["request"]


def assert_error(code, fn, *args, **kwargs):
    with pytest.raises(BusinessError) as failure:
        fn(*args, **kwargs)
    assert failure.value.code == code


def test_repair_complete_history_events_and_replay(business):
    store, room, desk = business
    order = repair(store, room, location="卧室", urgency="urgent")
    assert order["request_type"] == "repair" and order["version"] == 1
    assert order["repair"] == {"category": "air_conditioning", "location": "卧室", "urgency": "urgent", "related_request_id": None}
    submitted = store.inbox(desk)["events"]
    assert len(submitted) == 1 and submitted[0]["event_type"] == "hotel.repair.submitted"
    accepted = store.process_request(desk, order["id"], "accept", "十五分钟内检查", 1, "accept")
    store.process_request(desk, order["id"], "add_note", "已到场检查", 2, "note")
    completed = store.process_request(desk, order["id"], "complete", "更换部件，确认恢复制冷", 3, "complete")
    assert completed["request"]["status"] == "completed" and completed["request"]["version"] == 4
    # An old successful operation replays its original result even after later transitions.
    replay = HotelStore(store.path).process_request(desk, order["id"], "accept", "十五分钟内检查", 1, "accept")
    assert replay == {**accepted, "replayed": True}
    actions = store.list_actions(desk, order["id"])["actions"]
    assert [a["action"] for a in actions] == ["complete", "add_note", "accept", "submit"]
    assert [a["request_version"] for a in actions] == [4, 3, 2, 1]
    assert actions[0]["actor_principal_id"] == "desk"
    assert "actor_principal_id" not in store.list_actions(room, order["id"])["actions"][0]
    events = store.inbox(room)["events"]
    assert [e["event_type"] for e in events] == ["hotel.repair.accepted", "hotel.repair.updated", "hotel.repair.completed"]
    assert [e["request_version"] for e in events] == [2, 3, 4]
    assert all(e["speech_text"] and e["display_text"] and e["schema_version"] == 2 for e in events)
    assert_error("INVALID_TRANSITION", store.process_request, desk, order["id"], "add_note", "补充", 4, "later")


@pytest.mark.parametrize("terminal", ["reject", "cancel_room", "cancel_desk_pending", "cancel_desk_accepted", "close_unresolved"])
def test_exception_exits(business, terminal):
    store, room, desk = business
    order = repair(store, room)
    current = 1
    if terminal in ("close_unresolved", "cancel_desk_accepted"):
        store.process_request(desk, order["id"], "accept", "已安排检查", 1, "accept")
        current = 2
        assert_error("INVALID_TRANSITION", store.cancel_request, room, order["id"], "不需要了", 2, "guest-cancel")
    if terminal.startswith("cancel"):
        result = store.cancel_request(room if terminal == "cancel_room" else desk, order["id"], "确认取消", current, "end")
        expected = "cancelled"
    else:
        result = store.process_request(desk, order["id"], terminal, "无法处理，建议更换房间", current, "end")
        expected = "rejected" if terminal == "reject" else "closed_unresolved"
    assert result["request"]["status"] == expected
    assert_error("INVALID_TRANSITION", store.cancel_request, desk, order["id"], "重复结案", current + 1, "again")


def test_validation_transition_guards_and_idempotency_conflicts(business):
    store, room, desk = business
    order = repair(store, room)
    assert store.submit_repair(room, "repair-1", "air_conditioning", "空调不制冷")["replayed"]
    assert_error("IDEMPOTENCY_CONFLICT", store.submit_repair, room, "repair-1", "network", "空调不制冷")
    assert_error("IDEMPOTENCY_CONFLICT", store.submit, room, "repair-1", "空调不制冷")
    assert_error("VALIDATION_ERROR", store.transition, desk, order["id"], "accepted")
    assert_error("INVALID_TRANSITION", store.process_request, desk, order["id"], "complete", "修好了", 1, "early")
    for note in ("", "  ", "x" * 501):
        assert_error("VALIDATION_ERROR", store.process_request, desk, order["id"], "accept", note, 1, "bad")
    for invalid in (0, -1, True, "1"):
        assert_error("VALIDATION_ERROR", store.process_request, desk, order["id"], "accept", "处理", invalid, "bad")
    store.process_request(desk, order["id"], "accept", "处理", 1, "accept")
    assert_error("IDEMPOTENCY_CONFLICT", store.process_request, desk, order["id"], "accept", "其他安排", 1, "accept")
    assert_error("VERSION_CONFLICT", store.process_request, desk, order["id"], "complete", "修好", 1, "stale")
    assert_error("FORBIDDEN", store.process_request, room, order["id"], "complete", "修好", 2, "guest")


def test_stay_lifecycle_isolation_and_same_endpoint_key_reuse(business):
    store, room, desk = business
    order = repair(store, room)
    assert store.start_stay(desk, "101", "2099-01-01", "checkin")["replayed"]
    assert_error("INVALID_TRANSITION", store.start_stay, desk, "101", "2099-01-01", "duplicate")
    assert_error("NOT_FOUND", store.start_stay, desk, "999", "2099-01-01", "unknown")
    closed = store.end_stay(desk, room.stay_id, 1, "退房", "checkout")
    assert closed["open_request_count"] == 1
    assert store.end_stay(desk, room.stay_id, 1, "退房", "checkout")["replayed"]
    for fn, args in ((store.get_request, (order["id"],)), (store.list_requests, ()), (store.query_requests, ()),
                     (store.inbox, ()), (store.current_stay, ()), (store.list_actions, (order["id"],)),
                     (store.submit_repair, ("repair-1", "air_conditioning", "空调不制冷"))):
        assert_error("STAY_INACTIVE", fn, room, *args)
    stay2 = store.start_stay(desk, "101", "2099-01-02", "checkin2")["stay"]
    new_room = Principal("a", "room", "101", stay2["id"], "room")
    assert_error("NOT_FOUND", store.get_request, new_room, order["id"])
    assert store.inbox(new_room)["events"] == []
    # Same endpoint and request key in a new stay must never replay previous guests' data.
    new_order = repair(store, new_room)
    assert new_order["id"] != order["id"]
    store.process_request(desk, order["id"], "accept", "退房后维修", 1, "late-accept")
    store.process_request(desk, order["id"], "complete", "已修复", 2, "late-complete")
    assert store.inbox(new_room)["events"] == []
    assert store.get_request(desk, order["id"])["request"]["status"] == "completed"
    assert_error("STAY_INACTIVE", store.current_stay, Principal("a", "room", "101"))
    assert_error("STAY_INACTIVE", store.current_stay, Principal("a", "room", "102", stay2["id"]))


def test_cross_scope_and_filters(business):
    store, room, desk = business
    order = repair(store, room)
    other = Principal("b", "frontdesk", principal_id="other")
    assert_error("NOT_FOUND", store.get_request, other, order["id"])
    assert_error("NOT_FOUND", store.process_request, other, order["id"], "accept", "处理", 1, "other")
    assert_error("NOT_FOUND", store.end_stay, other, room.stay_id, 1, "退房", "other")
    assert store.query_requests(other)["requests"] == []
    assert_error("FORBIDDEN", store.query_requests, room, room="101")
    page = store.query_requests(desk, room="101", stay_id=room.stay_id, request_type="repair", statuses=["pending"],
                                created_from=order["created_at"], created_to=order["created_at"])
    assert [r["id"] for r in page["requests"]] == [order["id"]]
    assert_error("VALIDATION_ERROR", store.query_requests, desk, created_from="2026-01-01")


def test_cursor_snapshot_tampering_scope_and_history(business):
    store, room, desk = business
    orders = [repair(store, room, str(i)) for i in range(7)]
    first = store.query_requests(desk, request_type="repair", limit=3)
    repair(store, room, "new")
    second = HotelStore(store.path).query_requests(desk, request_type="repair", limit=3, cursor=first["next_cursor"])
    third = store.query_requests(desk, request_type="repair", limit=3, cursor=second["next_cursor"])
    assert [r["id"] for page in (first, second, third) for r in page["requests"]] == [r["id"] for r in reversed(orders)]
    assert third["next_cursor"] is None
    for kwargs in ({"cursor": first["next_cursor"]}, {"request_type": "repair", "cursor": "invalid"},
                   {"request_type": "repair", "cursor": first["next_cursor"][:-2] + "AA"}):
        assert_error("VALIDATION_ERROR", store.query_requests, desk, **kwargs)
    assert_error("VALIDATION_ERROR", store.query_requests, room, request_type="repair", cursor=first["next_cursor"])
    order = orders[0]
    store.process_request(desk, order["id"], "accept", "安排", 1, "accept")
    page1 = store.list_actions(desk, order["id"], limit=1)
    store.process_request(desk, order["id"], "add_note", "进度", 2, "note")
    page2 = store.list_actions(desk, order["id"], limit=1, cursor=page1["next_cursor"])
    assert [page1["actions"][0]["action"], page2["actions"][0]["action"]] == ["accept", "submit"]


@pytest.mark.parametrize("table", ["request_actions", "events", "operation_receipts"])
def test_failed_writes_roll_back_every_business_artifact(business, table):
    store, room, desk = business
    with store.connect() as db:
        db.execute(f"CREATE TRIGGER fail_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        repair(store, room)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM repair_details").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM request_actions").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM operation_receipts").fetchone()[0] == 1  # check-in only
        db.execute("DROP TRIGGER fail_write")
    order = repair(store, room)
    with store.connect() as db:
        db.execute(f"CREATE TRIGGER fail_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.process_request(desk, order["id"], "accept", "安排", 1, "accept")
    assert store.get_request(desk, order["id"])["request"]["version"] == 1
    assert len(store.list_actions(desk, order["id"])["actions"]) == 1
    assert store.inbox(room)["events"] == []


def test_concurrent_processing_and_submission(business):
    store, room, desk = business
    order = repair(store, room)
    barrier = Barrier(2)
    def accept(key):
        barrier.wait()
        try:
            store.process_request(desk, order["id"], "accept", "安排", 1, key)
            return "ok"
        except BusinessError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(accept, ["one", "two"])) == ["VERSION_CONFLICT", "ok"]
    barrier = Barrier(2)
    def submit(_):
        barrier.wait()
        return repair(store, room, "concurrent")["id"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results[0] == results[1]
    assert len(store.inbox(desk)["events"]) == 2


def legacy_database(path, broken=False):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE requests(id TEXT PRIMARY KEY,hotel TEXT NOT NULL,room TEXT NOT NULL,
                request_key TEXT NOT NULL,description TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
                note TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(hotel,room,request_key));
            CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT,hotel TEXT NOT NULL,
                role TEXT NOT NULL,room TEXT NOT NULL,payload TEXT NOT NULL);
            INSERT INTO requests(id,hotel,room,request_key,description) VALUES('old','a','101','repair-1','旧住客的请求');
        """)
        if broken:
            db.execute("UPDATE requests SET status='unknown'")
        for seq, role, room in ((3, "frontdesk", ""), (8, "room", "101"), (99, "frontdesk", "")):
            db.execute("INSERT INTO events VALUES(?,?,?,?,?)", (seq, "a", role, room, json.dumps({"event_type": "hotel.request.pending", "speech_text": "旧事件"})))
        db.execute("DELETE FROM events WHERE seq=99")


def test_explicit_legacy_migration_preserves_data_and_sequence(tmp_path):
    path = tmp_path / "legacy.db"
    legacy_database(path)
    with pytest.raises(ValueError, match="explicit --migrate"):
        HotelStore(path)
    store = HotelStore(path, migrate=True)
    HotelStore(path, migrate=True)  # idempotent upgrade
    desk = Principal("a", "frontdesk")
    old = store.get_request(desk, "old")["request"]
    assert old["description"] == "旧住客的请求" and old["request_type"] == "general" and old["stay_id"] is None
    assert store.query_requests(desk, created_from=old["created_at"], created_to=old["created_at"])["requests"][0]["id"] == "old"
    assert [e["seq"] for e in store.inbox(desk)["events"]] == [3]
    stay = store.start_stay(desk, "101", "2099-01-01", "checkin")["stay"]
    room = Principal("a", "room", "101", stay["id"])
    assert store.list_requests(room)["requests"] == []
    assert store.inbox(room)["events"] == []
    assert_error("NOT_FOUND", store.get_request, room, "old")
    repair(store, room)
    assert store.inbox(desk)["events"][-1]["seq"] > 99
    store.transition(desk, "old", "accepted", "历史工单处理")
    assert store.inbox(room)["events"] == []
    with store.connect() as db:
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_failed_migration_is_atomic(tmp_path):
    path = tmp_path / "broken.db"
    legacy_database(path, broken=True)
    with pytest.raises(sqlite3.IntegrityError):
        HotelStore(path, migrate=True)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT status FROM requests").fetchone()[0] == "unknown"
        assert "stay_id" not in [row[1] for row in db.execute("PRAGMA table_info(events)")]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='schema_migrations'").fetchall()


def test_cli_migration_and_private_binding(tmp_path):
    path = tmp_path / "hotel.db"
    config = tmp_path / "config.json"
    legacy_database(path)
    command = [sys.executable, "-m", "hotel_mcp"]
    denied = subprocess.run(command + ["--migrate"], capture_output=True, text=True)
    assert denied.returncode != 0
    migrated = subprocess.run(command + ["--database", str(path), "--migrate"], capture_output=True, text=True)
    assert migrated.returncode == 0, migrated.stderr
    initialized = subprocess.run(command + ["--config", str(config), "--init"], capture_output=True, text=True)
    assert initialized.returncode == 0, initialized.stderr
    settings = json.loads(config.read_text(encoding="utf-8"))
    settings["principals"][0]["hotel"] = "a"
    config.write_text(json.dumps(settings), encoding="utf-8")
    old_token = settings["principals"][0]["token"]
    store = HotelStore(path)
    stay = store.start_stay(Principal("a", "frontdesk"), "101", "2099-01-01", "checkin")["stay"]
    bound = subprocess.run(command + ["--config", str(config), "--database", str(path), "--bind-stay", stay["id"],
                                       "--principal-id", "room-101"], capture_output=True, text=True)
    assert bound.returncode == 0, bound.stderr
    saved = json.loads(config.read_text(encoding="utf-8"))["principals"][0]
    assert saved["stay_id"] == stay["id"] and saved["token"] != old_token
    assert old_token not in bound.stdout + bound.stderr and saved["token"] not in bound.stdout + bound.stderr


def test_related_repair_and_legacy_current_state(business):
    store, room, desk = business
    first = repair(store, room)
    second = repair(store, room, "repeat", related_request_id=first["id"])
    assert second["repair"]["related_request_id"] == first["id"]
    assert_error("NOT_FOUND", repair, store, room, "bad-related", related_request_id="unknown")
    general = store.submit(room, "general", "送水")
    assert_error("VALIDATION_ERROR", repair, store, room, "bad-general", related_request_id=general["id"])
    store.transition(desk, general["id"], "accepted")
    store.transition(desk, general["id"], "completed")
    assert store.submit(room, "general", "送水")["status"] == "completed"


def test_events_paginate_across_scope_gaps_and_room_disable(business):
    store, room, desk = business
    for n in range(53):
        order = repair(store, room, f"repair-{n}")
        store.process_request(desk, order["id"], "accept", "安排", 1, f"accept-{n}")
    first = store.inbox(room)
    second = store.inbox(room, first["next_cursor"])
    assert len(first["events"]) == 50 and first["has_more"]
    assert len(second["events"]) == 3 and not second["has_more"]
    assert first["events"][1]["seq"] - first["events"][0]["seq"] == 2
    assert store.inbox(room, second["next_cursor"])["next_cursor"] == second["next_cursor"]
    store.configure_rooms([{"hotel": "a", "room": "101", "enabled": False}])
    assert_error("STAY_INACTIVE", store.inbox, room)


def test_checkout_rolls_back_with_receipt_failure(business):
    store, room, desk = business
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_receipt BEFORE INSERT ON operation_receipts BEGIN SELECT RAISE(ABORT,'injected'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.end_stay(desk, room.stay_id, 1, "退房", "end")
    assert store.current_stay(room)["stay"]["status"] == "active"
