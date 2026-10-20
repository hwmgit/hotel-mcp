import asyncio
import json
import socket
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn
from websockets.asyncio.server import serve

from hotel_mcp.connections import destination
from hotel_mcp.domain import BusinessError
from hotel_mcp.server import create_app
from tests.test_services import services


@pytest_asyncio.fixture
async def setting_server(services):
    store, _, _, summary, config = services
    path = Path(summary["directory"]) / "config.json"
    app = create_app(config, store.path, config_path=path, allow_local_mcp=True)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=2))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.02)
        assert server.started
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{sock.getsockname()[1]}", trust_env=False) as http:
            yield http, config, path, app, store
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 8)
        sock.close()


def headers(config, actor="frontdesk-demo-central"):
    return {"Authorization": "Bearer " + next(p["token"] for p in config["principals"] if p["id"] == actor)}


def body(url, version=0, key="save-1"):
    return {"endpoint": url, "expected_version": version, "request_key": key}


def route(target="room-demo-central-101", actor="frontdesk-demo-central"):
    return f"/api/{actor}/connections/{target}"


async def handshake(ws):
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "hot-test", "version": "1"}}}))
    result = json.loads(await asyncio.wait_for(ws.recv(), 3))
    assert result["result"]["capabilities"]["resources"]["subscribe"]
    await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    result = json.loads(await asyncio.wait_for(ws.recv(), 3))
    names = {t["name"] for t in result["result"]["tools"]}
    assert "hotel_submit_repair" in names and "hotel_process_request" not in names
    await ws.send(json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                             "params": {"name": "hotel_get_current_stay", "arguments": {}}}))
    result = json.loads(await asyncio.wait_for(ws.recv(), 3))
    assert not result["result"].get("isError")
    assert json.loads(result["result"]["content"][0]["text"])["stay"]["room"] == "101"


@pytest.mark.asyncio
async def test_hot_save_test_replace_disable_restore_and_permissions(setting_server):
    http, config, path, app, store = setting_server
    received = asyncio.Queue()
    async def peer(ws):
        await received.put(ws)
        await ws.wait_closed()
    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp/?token=test-private"
        h = headers(config)
        saved = await http.post(route(), headers=h, json=body(url))
        assert saved.status_code == 200 and saved.json()["version"] == 1
        first = await asyncio.wait_for(received.get(), 3)
        await handshake(first)
        before = len(app.state.connections.tasks)
        tested = await http.post(route()+"/test", headers=h, json={})
        assert tested.json()["ok"] and received.empty() and len(app.state.connections.tasks) == before
        listing = await http.get("/api/frontdesk-demo-central/connections", headers=h)
        assert len(listing.json()["connections"]) == 3
        assert "test-private" not in listing.text and "test-private" not in (await http.get("/health")).text
        replay = await http.post(route(), headers=h, json=body(url))
        assert replay.json()["replayed"] and received.empty()
        conflict = await http.post(route(), headers=h, json=body(url+"x"))
        assert conflict.status_code == 409
        conflict = await http.post(route(), headers=h, json=body(url, key="stale"))
        assert conflict.status_code == 409
        duplicate = await http.post(route("room-demo-central-102"), headers=h, json=body(url, key="duplicate"))
        assert duplicate.status_code == 400
        cross = await http.post(route("room-demo-airport-101"), headers=h, json=body(url))
        assert cross.status_code == 404
        for actor in ("room-demo-central-101", "sales-manager"):
            r = await http.get(f"/api/{actor}/connections", headers=headers(config, actor))
            assert r.status_code == 403
        assert (await http.get("/api/frontdesk-demo-central/connections")).status_code == 401
        replaced = await http.post(route(), headers=h, json=body(url+"2", 1, "replace"))
        assert replaced.status_code == 200
        await asyncio.wait_for(first.wait_closed(), 3)
        second = await asyncio.wait_for(received.get(), 3)
        await handshake(second)
        # Restore from disk without changing the base config or losing the selected URL.
        restored = create_app(config, store.path, config_path=path, allow_local_mcp=True)
        assert restored.state.connections.records["room-demo-central-101"]["url"] == url+"2"
        assert restored.state.connections.records["room-demo-central-101"]["version"] == 2
        disabled = await http.post(route(), headers=h, json=body("", 2, "disable"))
        assert disabled.status_code == 200
        await asyncio.wait_for(second.wait_closed(), 3)
        assert not app.state.connections.tasks
        assert json.loads(path.with_name("mcp-connections.json").read_text())["connections"]["room-demo-central-101"]["url"] == ""
        assert "mcp_endpoint" not in json.loads(path.read_text())["principals"][-1]


@pytest.mark.asyncio
async def test_draft_probe_is_not_persisted_and_errors_are_redacted(setting_server):
    http, config, path, app, _ = setting_server
    received = asyncio.Queue()
    async def peer(ws):
        await received.put(ws)
        await ws.wait_closed()
    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp/?token=never-return-this"
        result = await http.post(route()+"/test", headers=headers(config), json={"endpoint": url})
        assert result.json()["ok"]
        ws = await asyncio.wait_for(received.get(), 2)
        await asyncio.wait_for(ws.wait_closed(), 2)
        assert not path.with_name("mcp-connections.json").exists() and not app.state.connections.tasks
    result = await http.post(route()+"/test", headers=headers(config), json={"endpoint": url})
    assert result.status_code == 200 and not result.json()["ok"] and "never-return-this" not in result.text
    malformed = await http.post(route()+"/test", headers=headers(config), json={"endpoint": "https://a/mcp/?token=secret"})
    assert malformed.status_code == 400 and "secret" not in malformed.text


@pytest.mark.asyncio
async def test_persistence_failure_keeps_live_connection(setting_server, monkeypatch):
    http, config, _, app, _ = setting_server
    queue = asyncio.Queue()
    async def peer(ws):
        await queue.put(ws)
        await ws.wait_closed()
    async with serve(peer, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp/"
        assert (await http.post(route(), headers=headers(config), json=body(url))).status_code == 200
        ws = await asyncio.wait_for(queue.get(), 3)
        def broken(*args):
            raise OSError("private-url-must-not-escape")
        monkeypatch.setattr(app.state.connections, "_persist", broken)
        failed = await http.post(route(), headers=headers(config), json=body("", 1, "disable"))
        assert failed.status_code == 503 and "private-url" not in failed.text
        assert app.state.connections.records["room-demo-central-101"]["version"] == 1
        await handshake(ws)


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["ws://127.0.0.1/mcp/", "wss://127.0.0.1/mcp/",
                                 "wss://169.254.169.254/mcp/", "wss://[::1]/mcp/"])
async def test_web_settings_reject_private_destinations(url):
    with pytest.raises(BusinessError):
        await destination(url)


@pytest.mark.asyncio
async def test_probe_rejects_redirect(setting_server):
    from websockets.http11 import Response
    from websockets.datastructures import Headers
    http, config, _, _, _ = setting_server
    async def redirect(connection, request):
        return Response(302, "Found", Headers({"Location": "ws://127.0.0.1:1/mcp/"}), b"")
    async with serve(lambda ws: ws.wait_closed(), "127.0.0.1", 0, process_request=redirect) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp/"
        result = await http.post(route()+"/test", headers=headers(config), json={"endpoint": url})
        assert not result.json()["ok"]


@pytest.mark.asyncio
async def test_simultaneous_saves_require_current_version(setting_server):
    http, config, _, app, _ = setting_server
    async with serve(lambda ws: ws.wait_closed(), "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp/"
        results = await asyncio.gather(*[
            http.post(route(), headers=headers(config), json=body(url+f"?token={i}", key=f"parallel-{i}"))
            for i in range(2)])
        assert sorted(r.status_code for r in results) == [200, 409]
        assert len(app.state.connections.tasks) == 1


@pytest.mark.asyncio
async def test_identity_named_test_uses_save_route(services):
    store, _, _, summary, config = services
    config["principals"].append({"id": "test", "role": "frontdesk", "hotel": "demo-central", "token": "t"*40})
    app = create_app(config, store.path, config_path=Path(summary["directory"]) / "config.json")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        result = await http.post(route("test"), headers=headers(config), json=body(""))
        assert result.status_code == 200 and result.json()["saved"]
