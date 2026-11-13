import asyncio
import hashlib
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import httpx
from starlette.testclient import TestClient

from hotel_mcp.accounts import Accounts, COOKIE, SESSION_SECONDS
from hotel_mcp.domain import BusinessError, Principal
from hotel_mcp.server import create_app
from hotel_mcp.store import HotelStore
from tests.test_services import services
from tests.test_business import assert_error
from tests.test_admin_protocol import service_server
from tests.test_hotel import client, result
from tests.protocol_client import CursorStore, consume

INITIAL = "test-initial-password-123"
PASSWORD = "test-changed-password-456"


def login_account(accounts, name, initial=INITIAL):
    token = accounts.login(name, initial, "local-test")
    actor = accounts.authenticate(token)
    if actor["must_change_password"]:
        accounts.change_password(actor, {"current_password": initial, "password": PASSWORD, "request_key": "first-password"})
        token = accounts.login(name, PASSWORD, "local-test")
        actor = accounts.authenticate(token)
    return actor, token


@pytest.fixture
def account_service(services):
    store, room, desk, summary, config = services
    accounts = Accounts(store)
    accounts.bootstrap("root-admin", INITIAL)
    root, token = login_account(accounts, "root-admin")
    return accounts, root, token, services


def create(accounts, root, login, role="frontdesk", hotel="demo-central"):
    return accounts.mutate(root, {"username": login, "display_name": login, "role": role, "hotel": hotel,
                                  "password": INITIAL, "request_key": "create-" + login})["account"]


def update_body(row, **changes):
    return {"display_name": row["display_name"], "role": row["role"], "hotel": row["hotel"],
            "enabled": bool(row["enabled"]), "expected_version": row["version"], "request_key": "update-" + row["id"], **changes}


def bind(accounts, root, customer, stay):
    return accounts.mutate(root, {"stay_id": stay, "expected_version": customer["version"], "request_key": "bind-" + customer["id"][:8] + stay},
                           customer["id"], "bind_stay")["account"]


def test_bootstrap_password_storage_and_no_config_import(account_service):
    accounts, root, token, (store, room, desk, _, config) = account_service
    assert_error("ALREADY_INITIALIZED", accounts.bootstrap, "another-root", INITIAL)
    assert_error("UNAUTHORIZED", accounts.login, config["principals"][0]["id"], config["principals"][0]["token"], "test")
    with store.connect() as db:
        rows = db.execute("SELECT * FROM accounts").fetchall()
        assert len(rows) == 1 and rows[0]["role"] == "super_admin"
        assert INITIAL not in str(dict(rows[0])) and PASSWORD not in str(dict(rows[0]))
        session = db.execute("SELECT * FROM web_sessions").fetchone()
        assert session["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
        assert token not in str(dict(session))
        assert 0 < session["expires_at"] - int(time.time()) <= SESSION_SECONDS
        assert all(INITIAL not in r["response_json"] and PASSWORD not in r["response_json"]
                   for r in db.execute("SELECT * FROM account_operations"))


def test_eight_character_passwords_create_change_reset_and_revoke(account_service):
    accounts, root, _, _ = account_service
    values = {"username": "eight-user", "display_name": "Eight", "role": "frontdesk", "hotel": "demo-central",
              "password": "1234567", "request_key": "eight-create"}
    assert_error("VALIDATION_ERROR", accounts.mutate, root, values)
    row = accounts.mutate(root, {**values, "password": "Pass1234"})["account"]
    token = accounts.login("eight-user", "Pass1234", "test-eight")
    actor = accounts.authenticate(token)
    change = {"current_password": "Pass1234", "password": "1234567", "request_key": "eight-change"}
    assert_error("VALIDATION_ERROR", accounts.change_password, actor, change)
    assert_error("VALIDATION_ERROR", accounts.change_password, actor, {**change, "password": "x" * 129})
    accounts.change_password(actor, {**change, "password": "New12345"})
    assert_error("UNAUTHORIZED", accounts.authenticate, token)
    token = accounts.login("eight-user", "New12345", "test-eight")
    actor = accounts.authenticate(token)
    assert not actor["must_change_password"]
    reset = {"password": "1234567", "expected_version": actor["version"], "request_key": "eight-reset"}
    assert_error("VALIDATION_ERROR", accounts.mutate, root, reset, row["id"], "reset_password")
    accounts.mutate(root, {**reset, "password": "Temp1234"}, row["id"], "reset_password")
    assert_error("UNAUTHORIZED", accounts.authenticate, token)
    token = accounts.login("eight-user", "Temp1234", "test-eight")
    assert accounts.authenticate(token)["must_change_password"]


def test_role_matrix_store_switch_and_account_management(account_service):
    accounts, root, _, (store, room, desk, _, _) = account_service
    for name, role in (("store-admin", "store_admin"), ("staff-user", "frontdesk"), ("guest-user", "customer")):
        create(accounts, root, name, role)
    manager, _ = login_account(accounts, "store-admin")
    staff, _ = login_account(accounts, "staff-user")
    guest, _ = login_account(accounts, "guest-user")
    session = accounts.session(root)
    assert len(session["stores"]) == 3 and "hotel_query_sales" in session["tools"]
    assert "hotel_process_request" not in session["tools"]
    assert_error("FORBIDDEN", accounts.execute, root, "", "", "hotel_query_requests", {})
    assert "hotel_process_request" in accounts.session(root, "demo-airport")["tools"]
    assert {"hotel_process_request", "hotel_query_sales"}.issubset(accounts.session(manager)["tools"])
    assert "hotel_process_request" in accounts.session(staff)["tools"]
    assert "hotel_query_sales" not in accounts.session(staff)["tools"]
    assert not accounts.session(guest)["tools"]
    for actor in (manager, staff, guest):
        assert_error("FORBIDDEN", accounts.session, actor, "demo-airport")
    for actor in (staff, guest):
        assert_error("FORBIDDEN", accounts.list_accounts, actor)
        assert_error("FORBIDDEN", create, accounts, actor, "unprivileged")
    assert_error("FORBIDDEN", create, accounts, manager, "escalated", "super_admin", "")
    assert_error("FORBIDDEN", create, accounts, manager, "other-store", "frontdesk", "demo-airport")
    assert_error("FORBIDDEN", create, accounts, manager, "peer-admin", "store_admin")
    assert {r["username"] for r in accounts.list_accounts(manager)["accounts"]} == {"staff-user", "guest-user"}
    assert_error("NOT_FOUND", accounts.mutate, manager, update_body(root, role="frontdesk", hotel="demo-central"), root["id"], "update")
    assert_error("FORBIDDEN", accounts.execute, staff, "", "", "hotel_query_sales", {"date_from":"2026-10-12", "date_to":"2026-10-18"})
    assert_error("FORBIDDEN", accounts.execute, manager, "", "", "hotel_query_sales",
                 {"date_from":"2026-10-12", "date_to":"2026-10-18", "hotel_ids":["demo-airport"]})
    order = store.query_requests(desk)["requests"][0]
    assert_error("NOT_FOUND", accounts.execute, root, "demo-airport", "", "hotel_get_request", {"request_id":order["id"]})


def test_super_admin_processes_selected_store_with_events_and_idempotency(account_service):
    accounts, root, _, (store, room, desk, _, _) = account_service
    order = store.submit_repair(room, "root-test", "network", "网络故障")["request"]
    args = {"request_id": order["id"], "action": "accept", "note": "总管理员已受理", "expected_version": 1, "request_key": "accept"}
    result = accounts.execute(root, desk.hotel, "", "hotel_process_request", args)
    assert result["request"]["status"] == "accepted"
    assert accounts.execute(root, desk.hotel, "", "hotel_process_request", args)["replayed"]
    assert store.inbox(room)["events"][-1]["event_type"] == "hotel.repair.accepted"
    with store.connect() as db:
        action = db.execute("SELECT actor_principal_id FROM request_actions WHERE request_id=? AND action='accept'", (order["id"],)).fetchone()
        assert action[0] == "account-" + root["id"]


def test_customer_history_survives_checkout_without_exposing_next_guest(account_service):
    accounts, root, _, (store, room, desk, _, _) = account_service
    customer = create(accounts, root, "guest-owner", "customer")
    customer = bind(accounts, root, customer, room.stay_id)
    actor, _ = login_account(accounts, "guest-owner")
    before = accounts.execute(actor, "", room.stay_id, "hotel_query_requests", {})
    assert before["requests"]
    other_stay = next(r for r in accounts.stay_options(root, customer["id"])["available"] if r["id"] != room.stay_id)
    assert_error("FORBIDDEN", accounts.session, actor, "", other_stay["id"])
    store.end_stay(desk, room.stay_id, 1, "退房", "checkout-customer")
    session = accounts.session(actor)
    assert session["history_readonly"] and "hotel_submit_repair" not in session["tools"]
    assert accounts.execute(actor, "", room.stay_id, "hotel_get_request", {"request_id": before["requests"][0]["id"]})
    assert accounts.execute(actor, "", room.stay_id, "hotel_list_request_actions", {"request_id": before["requests"][0]["id"]})
    assert_error("FORBIDDEN", accounts.execute, actor, "", room.stay_id, "hotel_submit_request", {"description":"退房后请求", "request_key":"late"})
    new_stay = store.start_stay(desk, room.room, "2099-12-31", "next-stay")["stay"]
    next_room = Principal(room.hotel, "room", room.room, new_stay["id"], "next-room")
    new_order = store.submit_repair(next_room, "next-repair", "network", "新客人的工单")["request"]
    assert_error("NOT_FOUND", accounts.execute, actor, "", room.stay_id, "hotel_get_request", {"request_id":new_order["id"]})
    next_customer = create(accounts, root, "next-customer", "customer")
    assert_error("VERSION_CONFLICT", bind, accounts, root, next_customer, room.stay_id)
    bind(accounts, root, next_customer, new_stay["id"])
    next_actor, _ = login_account(accounts, "next-customer")
    assert_error("NOT_FOUND", accounts.execute, next_actor, "", new_stay["id"], "hotel_get_request", {"request_id":before["requests"][0]["id"]})
    assert_error("FORBIDDEN", accounts.mutate, root, update_body(accounts.list_accounts(root, query="guest-owner")["accounts"][0], hotel="demo-airport"), customer["id"], "update")


def test_mutations_idempotency_revocation_and_concurrent_creation(account_service):
    accounts, root, root_token, (store, *_) = account_service
    row = create(accounts, root, "revocable")
    assert create(accounts, root, "revocable")["id"] == row["id"]
    actor, token = login_account(accounts, "revocable")
    p = accounts.context(actor)[0]
    current = accounts.list_accounts(root)["accounts"][-1]
    result = accounts.mutate(root, update_body(current, enabled=False), row["id"], "update")
    assert accounts.mutate(root, update_body(current, enabled=False), row["id"], "update")["replayed"]
    assert_error("IDEMPOTENCY_CONFLICT", accounts.mutate, root, update_body(current), row["id"], "update")
    assert_error("UNAUTHORIZED", accounts.authenticate, token)
    assert_error("UNAUTHORIZED", store.start_stay, p, "102", "2099-01-01", "stale-actor")
    assert_error("FORBIDDEN", accounts.mutate, root, update_body(root, enabled=False), root["id"], "update")
    assert accounts.authenticate(root_token)
    def concurrent(i):
        try:
            return accounts.mutate(root, {"username":"race-user", "display_name":"race", "role":"frontdesk", "hotel":"demo-central",
                                          "password":INITIAL, "request_key":f"race-{i}"})["account"]["username"]
        except BusinessError as error:
            return error.code
    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(concurrent, [1, 2])) == ["USERNAME_EXISTS", "race-user"]
    assert not result["account"]["enabled"]


def test_account_failure_rolls_back_audit_and_receipt(account_service, monkeypatch):
    accounts, root, _, (store, *_) = account_service
    def broken(*args):
        raise sqlite3.OperationalError("injected audit failure")
    monkeypatch.setattr(accounts, "_audit", broken)
    with pytest.raises(sqlite3.OperationalError):
        create(accounts, root, "rollback-user")
    with store.connect() as db:
        assert not db.execute("SELECT 1 FROM accounts WHERE username='rollback-user'").fetchone()
        assert not db.execute("SELECT 1 FROM account_operations WHERE request_key='create-rollback-user'").fetchone()


def test_ai_connections_preserve_manager_endpoints_with_scoped_admin_access(account_service):
    from hotel_mcp.connections import Connections
    accounts, root, _, (_, room, desk, _, _) = account_service
    create(accounts, root, "local-admin", "store_admin")
    create(accounts, root, "local-staff", "frontdesk")
    admin, _ = login_account(accounts, "local-admin")
    staff, _ = login_account(accounts, "local-staff")
    super_p = accounts.context(root, desk.hotel)[0]
    admin_p = accounts.context(admin)[0]
    staff_p = accounts.context(staff)[0]
    multi = Principal("", "manager", principal_id="sales-all", allowed_hotels=(desk.hotel, "demo-airport"))
    single = Principal("", "manager", principal_id="sales-single", allowed_hotels=(desk.hotel,))
    assert Connections.can_manage(super_p, multi)
    assert Connections.can_manage(admin_p, single)
    assert not Connections.can_manage(admin_p, multi)
    assert Connections.can_manage(admin_p, room)
    assert not Connections.can_manage(staff_p, room)
    assert not Connections.can_manage(super_p, Principal("demo-airport", "frontdesk"))


def test_returning_customer_switches_own_stays_and_keeps_old_history(account_service):
    accounts, root, _, (store, room, desk, _, _) = account_service
    customer = create(accounts, root, "returning-guest", "customer")
    customer = bind(accounts, root, customer, room.stay_id)
    actor, _ = login_account(accounts, "returning-guest")
    old_orders = accounts.execute(actor, "", room.stay_id, "hotel_query_requests", {})["requests"]
    store.end_stay(desk, room.stay_id, 1, "退房", "first-checkout")
    next_stay = store.start_stay(desk, room.room, "2099-12-31", "return-checkin")["stay"]
    customer = accounts.list_accounts(root, query="returning-guest")["accounts"][0]
    bind(accounts, root, customer, next_stay["id"])
    assert accounts.session(actor)["principal"]["stay_id"] == next_stay["id"]
    assert len(accounts.session(actor)["stays"]) == 2
    assert accounts.execute(actor, "", next_stay["id"], "hotel_query_requests", {})["requests"] == []
    assert accounts.session(actor, "", room.stay_id)["history_readonly"]
    assert accounts.execute(actor, "", room.stay_id, "hotel_get_request", {"request_id":old_orders[0]["id"]})
    extension = next(row for row in old_orders if row["request_type"] == "stay_extension")
    assert accounts.execute(actor, "", room.stay_id, "hotel_list_stay_extension_quotes", {"request_id":extension["id"]})
    new_order = accounts.execute(actor, "", next_stay["id"], "hotel_submit_request", {"description":"再次入住需要水", "request_key":"return-order"})
    assert new_order


def test_cookie_restore_restart_csrf_logout_and_expiry(account_service):
    accounts, root, _, (store, room, desk, _, config) = account_service
    with TestClient(create_app(config, store.path)) as web:
        assert web.get("/workspace/session").status_code == 401
        legacy = next(p for p in config["principals"] if p["id"] == desk.principal_id)
        assert web.get(f"/api/{desk.principal_id}/session", headers={"Authorization":"Bearer " + legacy["token"]}).status_code == 401
        assert web.post("/workspace/login", json={"username":"root-admin", "password":PASSWORD}).status_code == 403
        assert web.post("/workspace/login", headers={"X-Requested-With":"hotel-admin", "Origin":"https://attacker.test"},
                        json={"username":"root-admin", "password":PASSWORD}).status_code == 403
        login = web.post("/workspace/login", headers={"X-Requested-With":"hotel-admin"}, json={"username":"root-admin", "password":PASSWORD})
        assert login.status_code == 200
        cookie = login.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and f"Max-Age={SESSION_SECONDS}" in cookie
        session = web.get("/workspace/session").json()
        assert web.get("/workspace/session").json()["account"]["id"] == root["id"]
        headers = {"X-Requested-With":"hotel-admin", "X-CSRF-Token":session["csrf_token"]}
        assert web.post("/workspace/logout", json={}, headers={"X-Requested-With":"hotel-admin"}).status_code == 403
        token = web.cookies[COOKIE]
    with TestClient(create_app(config, store.path)) as restarted:
        restarted.cookies.set(COOKIE, token)
        assert restarted.get("/workspace/session").status_code == 200
        assert restarted.post("/workspace/logout", json={}, headers=headers).status_code == 200
        restarted.cookies.set(COOKIE, token)
        assert restarted.get("/workspace/session").status_code == 401
        token = accounts.login("root-admin", PASSWORD, "test")
        with store.connect() as db:
            db.execute("UPDATE web_sessions SET expires_at=0")
        restarted.cookies.set(COOKIE, token)
        assert restarted.get("/workspace/session").status_code == 401
    with TestClient(create_app(config, store.path), base_url="https://hotel.example") as secure:
        result = secure.post("/workspace/login", headers={"X-Requested-With":"hotel-admin"}, json={"username":"root-admin", "password":PASSWORD})
        assert "Secure" in result.headers["set-cookie"]


def test_forced_password_reset_and_api_boundaries(account_service):
    accounts, root, _, (store, room, desk, _, config) = account_service
    row = create(accounts, root, "web-staff")
    with TestClient(create_app(config, store.path)) as web:
        headers = {"X-Requested-With":"hotel-admin"}
        assert web.post("/workspace/login", headers=headers, json={"username":"web-staff", "password":INITIAL}).status_code == 200
        session = web.get("/workspace/session").json()
        assert session["account"]["must_change_password"] and not session["tools"]
        headers["X-CSRF-Token"] = session["csrf_token"]
        assert web.get("/workspace/accounts").status_code == 403
        assert web.post("/workspace/tools/hotel_query_requests", json={}, headers=headers).status_code == 403
        assert web.post("/workspace/password", headers=headers, json={"current_password":INITIAL, "password":PASSWORD, "request_key":"change"}).status_code == 200
        assert web.get("/workspace/session").status_code == 401
        assert web.post("/workspace/login", headers=headers, json={"username":"web-staff", "password":PASSWORD}).status_code == 200
        headers["X-CSRF-Token"] = web.get("/workspace/session").json()["csrf_token"]
        assert web.post("/workspace/tools/hotel_query_requests", json={}, headers=headers).status_code == 200
        assert web.post("/workspace/tools/hotel_query_sales", json={}, headers=headers).status_code == 403
        assert web.get("/workspace/accounts").status_code == 403
        assert web.get("/workspace/connections").status_code == 403
        assert web.get("/workspace/session", headers={"X-Hotel-ID":"demo-airport"}).status_code == 403
        current = accounts.list_accounts(root, query="web-staff")["accounts"][0]
        accounts.mutate(root, {"password":INITIAL, "expected_version":current["version"], "request_key":"reset"}, row["id"], "reset_password")
        assert web.get("/workspace/session").status_code == 401
        assert web.post("/workspace/login", headers=headers, json={"username":"web-staff", "password":PASSWORD}).status_code == 401
        assert web.post("/workspace/login", headers=headers, json={"username":"web-staff", "password":INITIAL}).status_code == 200
        assert web.get("/workspace/session").json()["account"]["must_change_password"]
        assert web.post("/workspace/register", json={}).status_code == 404


def test_login_rate_limit(account_service):
    accounts, _, _, _ = account_service
    for _ in range(10):
        assert_error("UNAUTHORIZED", accounts.login, "missing-user", "wrong", "test")
    assert_error("RATE_LIMITED", accounts.login, "missing-user", "wrong", "test")


@pytest.mark.asyncio
async def test_account_web_processing_preserves_real_mcp_notification(service_server, tmp_path):
    base, connections, (store, room, desk, _, _) = service_server
    accounts = Accounts(store)
    accounts.bootstrap("web-admin", INITIAL)
    _, token = login_account(accounts, "web-admin")
    events = asyncio.Queue()
    cursors = CursorStore(tmp_path / "account-events.db")
    cursors.ack("room", store.latest_event_seq(room))
    consumer = asyncio.create_task(consume(connections[room.principal_id], cursors, "room", events.put, poll_seconds=60))
    try:
        await asyncio.sleep(.4)
        async with client(connections[room.principal_id]) as (guest, _), httpx.AsyncClient(base_url=base, trust_env=False) as web:
            order = result(await guest.call_tool("hotel_submit_repair", {"request_key":"new-account-mcp", "category":"network", "description":"网络故障"}))["request"]
            web.cookies.set(COOKIE, token)
            session = (await web.get("/workspace/session")).json()
            headers = {"X-Requested-With":"hotel-admin", "X-CSRF-Token":session["csrf_token"], "X-Hotel-ID":desk.hotel}
            response = await web.post("/workspace/tools/hotel_process_request", headers=headers,
                                      json={"request_id":order["id"], "action":"accept", "note":"总管理员已安排", "expected_version":1, "request_key":"accept-mcp"})
            assert response.status_code == 200
            event = await asyncio.wait_for(events.get(), 3)
            assert event["event_type"] == "hotel.repair.accepted" and event["request_id"] == order["id"]
            assert result(await guest.call_tool("hotel_get_request", {"request_id":order["id"]}))["request"]["status"] == "accepted"
            assert (await web.get(f"/api/{desk.principal_id}/session", headers=connections[desk.principal_id]["headers"])).status_code == 401
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)


@pytest.mark.parametrize("broken", [False, True])
def test_v6_upgrade_is_additive_explicit_and_atomic(services, monkeypatch, broken):
    store, room, desk, _, _ = services
    from hotel_mcp import account_schema
    with store.connect() as db:
        for table in ("account_audit", "account_operations", "web_sessions", "account_stays", "accounts"):
            db.execute(f"DROP TABLE {table}")
        db.execute("DELETE FROM schema_migrations WHERE version=7")
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='schema_migrations'")]
        before = {t:[tuple(r) for r in db.execute(f'SELECT * FROM "{t}"')] for t in tables}
    with pytest.raises(ValueError, match="explicit --migrate"):
        HotelStore(store.path)
    if broken:
        monkeypatch.setattr(account_schema, "ACCOUNT_SCHEMA", [*account_schema.ACCOUNT_SCHEMA, "INSERT INTO missing VALUES(1)"])
        with pytest.raises(sqlite3.OperationalError):
            HotelStore(store.path, migrate=True)
    else:
        HotelStore(store.path, migrate=True)
        HotelStore(store.path, migrate=True)
    with store.connect() as db:
        assert db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == (6 if broken else 7)
        assert {t:[tuple(r) for r in db.execute(f'SELECT * FROM "{t}"')] for t in tables} == before
        assert not db.execute("PRAGMA foreign_key_check").fetchall()

