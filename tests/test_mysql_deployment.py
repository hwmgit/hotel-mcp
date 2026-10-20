"""Opt-in real MySQL 8 tests in an owned, disposable loopback Docker container."""
import asyncio
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pymysql
import pytest
import uvicorn
import yaml

from hotel_mcp import mysql_schema
from hotel_mcp.deployment import SEED_KEY, prepare, seed
from hotel_mcp.domain import BusinessError, Principal
from hotel_mcp.mysql import query_sql
from hotel_mcp.server import create_app
from hotel_mcp.store import HotelStore
from tests.test_hotel import client, result


def test_mysql_query_parameters_are_not_rewritten_inside_literals():
    assert query_sql("SELECT '?' AS label, value FROM service_metadata WHERE key=?") == \
        "SELECT '?' AS label, value FROM service_metadata WHERE `key`=%s"


def test_controlled_compose_contract():
    root = Path(__file__).parents[1]
    compose = yaml.safe_load((root / "docker-compose.coolify.yml").read_text(encoding="utf-8"))
    manifest = compose["x-hzyz-deploy"]
    assert manifest["application"]["type"] == "rds"
    assert manifest["build"] == {"node_artifact": "local-dist", "artifact_path": "/hotel_mcp/web"}
    assert (root / manifest["build"]["artifact_path"].lstrip("/") / "index.html").is_file()
    assert manifest["database"]["migration_command"] == "python -m hotel_mcp.deployment prepare"
    assert {v["name"] for v in manifest["environment"]["variables"]} == \
        {"DB_HOST", "DB_PORT", "DB_DATABASE", "DB_USERNAME", "DB_PASSWORD"}
    assert all(v["source"] == "generated" for v in manifest["environment"]["variables"])
    assert not any("ports" in service for service in compose["services"].values())
    assert compose["services"]["hotel"]["volumes"] == ["hotel-private:/data"]
    assert "!hotel_mcp/web/" in (root / ".dockerignore").read_text()


@pytest.fixture(scope="session")
def mysql_container(tmp_path_factory):
    if os.environ.get("HOTEL_TEST_MYSQL") != "1":
        pytest.skip("Set HOTEL_TEST_MYSQL=1 to run disposable MySQL 8 integration tests")
    root = tmp_path_factory.mktemp("mysql-container")
    password = secrets.token_urlsafe(32)
    env_file = root / "mysql.env"
    env_file.write_text("MYSQL_ROOT_PASSWORD=" + password + "\nMYSQL_ROOT_HOST=%\n", encoding="utf-8")
    name = "hotel-test-" + uuid.uuid4().hex[:12]
    started = False
    try:
        process = subprocess.run(["docker", "run", "-d", "--rm", "--name", name,
                                  "--env-file", str(env_file), "-p", "127.0.0.1::3306",
                                  "--tmpfs", "/var/lib/mysql:rw,size=1g", "mysql:8.0"],
                                 capture_output=True, text=True, timeout=90)
        assert process.returncode == 0, "Unable to start isolated MySQL test container"
        started = True
        port_output = subprocess.check_output(["docker", "port", name, "3306/tcp"], text=True)
        port = int(port_output.strip().split(":")[-1])
        options = dict(host="127.0.0.1", port=port, user="root", password=password, charset="utf8mb4")
        for _ in range(90):
            try:
                with pymysql.connect(**options) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT VERSION()")
                        assert cursor.fetchone()[0].startswith("8.0.")
                break
            except pymysql.MySQLError:
                time.sleep(1)
        else:
            pytest.fail("Isolated MySQL did not become ready")
        yield options
    finally:
        if started:
            subprocess.run(["docker", "stop", "--time", "3", name], capture_output=True, timeout=20)


@pytest.fixture
def mysql_database(mysql_container, monkeypatch):
    database = "hotel_test_" + uuid.uuid4().hex[:16]
    username, password = "ht_" + uuid.uuid4().hex[:12], secrets.token_urlsafe(32)
    with pymysql.connect(**mysql_container, autocommit=True) as raw:
        with raw.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin")
            cursor.execute("CREATE USER %s@'%%' IDENTIFIED BY %s", (username, password))
            cursor.execute(f"GRANT ALL PRIVILEGES ON `{database}`.* TO %s@'%%'", (username,))
    for key, value in {"DB_HOST": mysql_container["host"], "DB_PORT": str(mysql_container["port"]),
                       "DB_DATABASE": database, "DB_USERNAME": username, "DB_PASSWORD": password}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DB_SSL_CA", raising=False)
    yield database
    # The owned container is removed after this test session; no external DB cleanup.


@pytest.fixture
def mysql_demo(mysql_database, tmp_path):
    config, summary = prepare(tmp_path / "private")
    store = HotelStore("mysql")
    def principal(ident):
        p = next(p for p in config["principals"] if p["id"] == ident)
        return Principal(p.get("hotel", ""), p["role"], p.get("room", ""), p.get("stay_id", ""),
                         p["id"], tuple(p.get("allowed_hotels", [])))
    return store, config, summary, principal, tmp_path / "private"


def apply(store, desk, row, action, key=None):
    return store.process_request(desk, row["id"], action, "人工确认", row["version"], key or uuid.uuid4().hex)["request"]


def test_mysql_seed_five_stores_repeat_preserves_data_and_credentials(mysql_demo):
    store, config, summary, principal, root = mysql_demo
    assert summary["sales_order_count"] == 600 and summary["request_count"] == 25
    manager = principal("sales-manager")
    stores = store.sales.list_stores(manager)["stores"]
    assert len(stores) == 5
    assert all("演示" not in s["name"] for s in stores)
    sales = store.sales.query(manager, stores[0]["available_from"], stores[0]["available_to"])
    assert sales["is_demo"] and sales["totals"]["order_count"] == 600
    assert all(s["order_count"] == 120 and s["sales_amount_minor"] > 0 for s in sales["by_store"])
    assert isinstance(sales["totals"]["sales_amount_minor"], int)
    guest, desk = principal("room-demo-central-101"), principal("frontdesk-demo-central")
    row = store.query_requests(guest, request_type="repair")["requests"][0]
    completed = apply(store, desk, apply(store, desk, row, "accept"), "complete")
    before = store.inbox(guest)
    private_before = (root / "bootstrap/config.json").read_bytes()
    _, again = prepare(root)
    assert not again["seeded"] and private_before == (root / "bootstrap/config.json").read_bytes()
    assert store.get_request(desk, completed["id"])["request"]["status"] == "completed"
    assert store.inbox(guest) == before
    edited = json.loads(private_before)
    edited["principals"][0]["token"] = secrets.token_urlsafe(32)
    edited["principals"][-1]["stay_id"] = "changed-by-maintenance"
    (root / "bootstrap/config.json").write_text(json.dumps(edited), encoding="utf-8")
    assert not prepare(root)[1]["seeded"]
    with pytest.raises(ValueError, match="volume missing"):
        prepare(root.parent / "lost-volume")
    with pytest.raises(BusinessError):
        store.sales.query(principal("frontdesk-demo-central"), stores[0]["available_from"], stores[0]["available_to"],
                          hotel_ids=["demo-lake"])


def test_mysql_five_business_flows_and_unicode(mysql_demo):
    store, _, _, principal, _ = mysql_demo
    room, desk = principal("room-demo-central-101"), principal("frontdesk-demo-central")
    room2 = principal("room-demo-central-102")
    for kind, guest in (("repair", room), ("meal", room), ("gym_booking", room), ("meeting_booking", room2)):
        row = store.query_requests(guest, request_type=kind)["requests"][0]
        done = apply(store, desk, apply(store, desk, row, "accept"), "complete")
        assert done["status"] == "completed"
        assert len(store.list_actions(guest, row["id"])["actions"]) == 3
    ext = store.query_requests(room, request_type="stay_extension")["requests"][0]
    quoted = store.extensions.quote(desk, ext["id"], 60000, "两晚含早餐 ☕", ext["version"], "quote")["request"]
    qid = quoted["extension"]["current_quote"]["id"]
    confirmed = store.extensions.confirm(room, ext["id"], qid, quoted["version"], "confirm")["request"]
    processing = store.extensions.begin(desk, ext["id"], qid, "已核实", confirmed["version"], "begin")["request"]
    done = store.extensions.finish(desk, ext["id"], qid, "success", "已办理", processing["version"], "finish")["request"]
    assert done["status"] == "completed"
    assert store.current_stay(room)["stay"]["expected_checkout_date"] == done["extension"]["requested_checkout_date"]
    row = store.submit_repair(room, "unicode", "other", "特殊文字 ? % ' 😀")["request"]
    assert row["description"] == "特殊文字 ? % ' 😀"
    assert store.entities.resolve(principal("sales-manager"), "市区店")["status"] == "needs_clarification"


def test_mysql_concurrent_booking_and_idempotency(mysql_demo):
    store, _, _, principal, _ = mysql_demo
    room, desk = principal("room-demo-central-101"), principal("frontdesk-demo-central")
    with store.connect() as db:
        slot = db.execute("SELECT id FROM booking_slots WHERE hotel=? AND resource_id=? ORDER BY id DESC",
                          (room.hotel, "demo-central-meeting")).fetchone()[0]
    rows = [store.services.submit_booking(room, slot, 3, f"booking-{i}")["request"] for i in range(2)]
    def confirm(row):
        try:
            return apply(store, desk, row, "accept")["status"]
        except BusinessError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(confirm, rows)) == ["RESOURCE_UNAVAILABLE", "accepted"]
    def submit(_):
        return store.services.submit_meal(room, [{"item_id": "demo-central-noodles", "quantity": 1}], "same-key")
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(submit, range(2)))
    assert len({r["request"]["id"] for r in outcomes}) == 1
    assert sorted(r["replayed"] for r in outcomes) == [False, True]


def test_mysql_failed_receipt_rolls_back_status_history_events(mysql_demo, monkeypatch):
    store, _, _, principal, _ = mysql_demo
    room, desk = principal("room-demo-central-101"), principal("frontdesk-demo-central")
    row = store.query_requests(room, request_type="meal")["requests"][0]
    history, inbox = store.list_actions(room, row["id"]), store.inbox(room)
    from hotel_mcp.mysql import Connection
    execute = Connection.execute
    def injected(self, sql, args=()):
        if sql.startswith("INSERT INTO operation_receipts"):
            raise sqlite3.OperationalError("injected")
        return execute(self, sql, args)
    monkeypatch.setattr(Connection, "execute", injected)
    with pytest.raises(sqlite3.Error):
        apply(store, desk, row, "accept")
    assert store.get_request(room, row["id"])["request"]["status"] == "pending"
    assert store.list_actions(room, row["id"]) == history and store.inbox(room) == inbox


def test_mysql_partial_migration_recovers_and_checks_ownership(mysql_database, monkeypatch):
    original = mysql_schema.SCHEMA
    monkeypatch.setattr(mysql_schema, "SCHEMA", original[:5] + ["INVALID SQL"])
    with pytest.raises(sqlite3.Error):
        HotelStore("mysql", migrate=True)
    monkeypatch.setattr(mysql_schema, "SCHEMA", original)
    store = HotelStore("mysql", migrate=True)
    before = store.cursor_secret
    assert HotelStore("mysql", migrate=True).cursor_secret == before
    with store.connect() as db:
        db.execute("UPDATE service_metadata SET value='other' WHERE key='storage_backend'")
    with pytest.raises(ValueError, match="unowned"):
        HotelStore("mysql", migrate=True)


def test_mysql_slot_overlap_and_changed_seed_are_rejected(mysql_demo):
    store, config, _, _, root = mysql_demo
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        slot = dict(db.execute("SELECT * FROM booking_slots LIMIT 1").fetchone())
        slot["id"] = "overlapping-slot"
        with pytest.raises(BusinessError):
            store.services.insert_slot(db, slot)
    # A separate owned database is unnecessary: reject an altered seed without changing rows.
    source = root / "bootstrap/hotel.sqlite3"
    altered = root / "altered.sqlite3"
    altered.write_bytes(source.read_bytes() + b"altered")
    with pytest.raises(ValueError, match="identity mismatch"):
        seed(store, altered, config)
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 5
        assert db.execute("SELECT COUNT(*) FROM booking_slots WHERE id='overlapping-slot'").fetchone()[0] == 0


def test_built_image_with_mysql_and_private_volume(mysql_database, tmp_path):
    image = os.environ.get("HOTEL_TEST_IMAGE")
    if not image:
        pytest.skip("Set HOTEL_TEST_IMAGE to validate the production container lifecycle")
    name = "hotel-image-test-" + uuid.uuid4().hex[:12]
    volume = name + "-private"
    env_file = tmp_path / "app.env"
    variables = {k: os.environ[k] for k in ("DB_HOST", "DB_PORT", "DB_DATABASE", "DB_USERNAME", "DB_PASSWORD")}
    variables["DB_HOST"] = "host.docker.internal"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in variables.items()), encoding="utf-8")
    subprocess.run(["docker", "volume", "create", volume], check=True, capture_output=True)
    try:
        started = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "--env-file", str(env_file),
                                  "-p", "127.0.0.1::8120", "-v", volume + ":/data", image,
                                  "python", "-m", "hotel_mcp.deployment", "serve"], capture_output=True, timeout=60)
        assert started.returncode == 0, "Image startup failed"
        port = subprocess.check_output(["docker", "port", name, "8120/tcp"], text=True).strip().split(":")[-1]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as http:
            for _ in range(60):
                try:
                    if http.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(1)
            else:
                pytest.fail("Built image never became healthy")
            assert http.get("/admin/").status_code == 200
            assert http.get("/admin/assets/app.js").status_code == 200
        hook = subprocess.run(["docker", "exec", name, "python", "-m", "hotel_mcp.deployment", "prepare"],
                              capture_output=True, text=True, timeout=30)
        assert hook.returncode == 0 and json.loads(hook.stdout)["seeded"] is False
        with HotelStore("mysql").connect() as db:
            assert db.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 5
            assert db.execute("SELECT COUNT(*) FROM demo_sales_orders").fetchone()[0] == 600
    finally:
        subprocess.run(["docker", "stop", "--time", "3", name], capture_output=True, timeout=20)
        subprocess.run(["docker", "volume", "rm", volume], capture_output=True, timeout=20)


@pytest.mark.asyncio
async def test_mysql_http_room_and_web_frontdesk(mysql_demo):
    store, config, _, principal, _ = mysql_demo
    app = create_app(config, "mysql")
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
        guest = next(p for p in config["principals"] if p["id"] == "room-demo-central-101")
        desk = next(p for p in config["principals"] if p["id"] == "frontdesk-demo-central")
        guest_config = {"url": f"{base}/{guest['id']}/mcp", "headers": {"Authorization": "Bearer " + guest["token"]}}
        async with client(guest_config) as (session, notifications), httpx.AsyncClient(base_url=base, trust_env=False) as http:
            order = result(await session.call_tool("hotel_submit_meal_order", {
                "items": [{"item_id": "demo-central-noodles", "quantity": 2}], "request_key": "mysql-http"}))["request"]
            response = await http.post(f"/api/{desk['id']}/tools/hotel_process_request",
                                       headers={"Authorization": "Bearer " + desk["token"]},
                                       json={"request_id": order["id"], "action": "accept", "note": "已确认厨房可制作",
                                             "expected_version": 1, "request_key": "mysql-http-accept"})
            assert response.status_code == 200 and response.json()["request"]["status"] == "accepted"
            assert result(await session.call_tool("hotel_get_request", {"request_id": order["id"]}))["request"]["status"] == "accepted"
            assert (await http.get("/health")).status_code == 200
            assert (await http.get("/admin/assets/app.js")).status_code == 200
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)
        sock.close()
