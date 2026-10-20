"""Private, single-process MCP connection settings and hot connection lifecycle."""
import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .domain import fail
from .outbound import DirectConnect, run_connector, validate_endpoint


def fingerprint(url):
    p = urlsplit(url)
    return (p.scheme, p.hostname, p.port or (443 if p.scheme == "wss" else 80),
            p.path.rstrip("/"), tuple(sorted(parse_qsl(p.query, keep_blank_values=True))))


async def destination(url, allow_local=False):
    try:
        validate_endpoint(url)
        p = urlsplit(url)
        if p.scheme != "wss" and not allow_local:
            raise ValueError()
        answers = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(
            p.hostname, p.port or (443 if p.scheme == "wss" else 80), type=socket.SOCK_STREAM), 5)
        addresses = [ipaddress.ip_address(a[4][0]) for a in answers]
        if not addresses or any(not a.is_global and not (allow_local and a.is_loopback) for a in addresses):
            raise ValueError()
        options = {"host": str(addresses[0]), "port": p.port or (443 if p.scheme == "wss" else 80)}
        if p.scheme == "wss":
            options["server_hostname"] = p.hostname
        return options
    except (ValueError, OSError, TimeoutError):
        fail("VALIDATION_ERROR", "接入点须为可解析的公网 WSS MCP 地址；不支持内网地址")


class Connections:
    def __init__(self, endpoints, config, config_path=None, allow_local=False):
        self.endpoints = {e.principal.principal_id: e for e in endpoints}
        self.path = Path(config_path).with_name("mcp-connections.json") if config_path else None
        self.allow_local = allow_local
        self.lock = asyncio.Lock()
        self.active = False
        self.tasks, self.sockets, self.status = {}, {}, {}
        self.records = {p["id"]: {"url": p.get("mcp_endpoint", ""), "version": 0, "managed": False}
                        for p in config["principals"]}
        self.receipts = []
        if self.path and self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("schema_version") != 1:
                raise ValueError("Unsupported private MCP settings version")
            self.receipts = saved.get("receipts", [])
            for key, value in saved["connections"].items():
                if key in self.records:
                    self.records[key] = {**value, "managed": True}
        urls = set()
        for key, record in self.records.items():
            if record["url"]:
                validate_endpoint(record["url"])
                mark = fingerprint(record["url"])
                if mark in urls:
                    raise ValueError("Each MCP endpoint must belong to exactly one business identity")
                urls.add(mark)
                self.status[key] = {"state": "stopped"}

    def authorize(self, actor, target=None):
        if actor.role != "frontdesk":
            fail("FORBIDDEN", "仅前台可管理本店 AI 接入配置")
        if not self.path:
            fail("FORBIDDEN", "当前实例未配置可持久化的配置文件路径")
        if target is not None:
            endpoint = self.endpoints.get(target)
            if not endpoint or endpoint.principal.hotel != actor.hotel or endpoint.principal.role not in ("room", "frontdesk"):
                fail("NOT_FOUND", "找不到本店可管理的业务身份")

    def snapshot(self, actor):
        self.authorize(actor)
        rows = []
        for key, endpoint in self.endpoints.items():
            p = endpoint.principal
            if p.hotel != actor.hotel or p.role not in ("room", "frontdesk"):
                continue
            record = self.records[key]
            parsed = urlsplit(record["url"]) if record["url"] else None
            # Paths and query parameters may both contain credentials.
            masked = f"{parsed.scheme}://{parsed.hostname}/…/mcp/（凭据已隐藏）" if parsed else ""
            rows.append({"id": key, "role": p.role, "room": p.room, "version": record["version"],
                         "configured": bool(parsed), "masked_endpoint": masked,
                         "state": self.status.get(key, {}).get("state", "disabled")})
        return {"connections": rows}

    async def _launch(self, key):
        record = self.records[key]
        if not self.active or not record["url"]:
            return
        status = self.status[key] = {"state": "connecting"}
        # Managed URLs are DNS checked and pinned for each retry.
        self.tasks[key] = asyncio.create_task(run_connector(
            self.endpoints[key], record["url"], status,
            resolve=(lambda url: destination(url, self.allow_local)) if record["managed"] else None,
            on_socket=lambda ws: self.sockets.__setitem__(key, ws)))

    async def _stop(self, key):
        task = self.tasks.pop(key, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.sockets.pop(key, None)
        self.status.pop(key, None)

    async def start(self):
        async with self.lock:
            self.active = True
            for key in self.records:
                await self._launch(key)

    async def close(self):
        async with self.lock:
            self.active = False
            for key in list(self.tasks):
                await self._stop(key)

    def _persist(self, records, receipts):
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                temporary.chmod(0o600)
                json.dump({"schema_version": 1,
                           "connections": {k: v for k, v in records.items() if v["managed"]},
                           "receipts": receipts}, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    async def save(self, actor, target, body):
        self.authorize(actor, target)
        if (not isinstance(body, dict) or set(body) != {"endpoint", "expected_version", "request_key"}
                or not isinstance(body["endpoint"], str) or len(body["endpoint"]) > 4096
                or type(body["expected_version"]) is not int or body["expected_version"] < 0
                or not isinstance(body["request_key"], str) or not 1 <= len(body["request_key"]) <= 80):
            fail("VALIDATION_ERROR", "请提供接入地址、配置版本和操作编号")
        url = body["endpoint"].strip()
        digest = hashlib.sha256(json.dumps([target, body], sort_keys=True).encode()).hexdigest()
        async with self.lock:
            for receipt in self.receipts:
                if (receipt["actor"], receipt["key"]) == (actor.principal_id, body["request_key"]):
                    if receipt["digest"] != digest:
                        fail("IDEMPOTENCY_CONFLICT", "同一操作编号不能用于不同配置")
                    return {"saved": True, "version": receipt["version"], "replayed": True}
            previous = self.records[target]
            if body["expected_version"] != previous["version"]:
                fail("VERSION_CONFLICT", "配置已更新，请刷新后再保存")
            if url:
                await destination(url, self.allow_local)
                self._unique(target, url)
            version = previous["version"] + 1
            records = {**self.records, target: {"url": url, "version": version, "managed": True}}
            receipts = [*self.receipts, {"actor": actor.principal_id, "key": body["request_key"],
                                       "digest": digest, "version": version}]
            try:
                self._persist(records, receipts)
            except OSError:
                fail("TEMPORARY_UNAVAILABLE", "配置保存失败，原连接保持不变，请重试")
            self.records, self.receipts = records, receipts
            await self._stop(target)
            await self._launch(target)
            return {"saved": True, "version": version, "replayed": False}

    def _unique(self, target, url):
        if any(key != target and row["url"] and fingerprint(row["url"]) == fingerprint(url)
               for key, row in self.records.items()):
            fail("VALIDATION_ERROR", "该接入点已绑定其他业务身份，请使用独立接入点")

    async def test(self, actor, target, body):
        self.authorize(actor, target)
        if not isinstance(body, dict) or set(body) - {"endpoint"}:
            fail("VALIDATION_ERROR", "测试参数不正确")
        async with self.lock:
            url = body.get("endpoint", self.records[target]["url"])
            if not isinstance(url, str) or not url.strip():
                fail("VALIDATION_ERROR", "请先填写或保存接入点")
            url = url.strip()
            options = await destination(url, self.allow_local)
            self._unique(target, url)
            try:
                async with asyncio.timeout(12):
                    existing = self.records[target]["url"]
                    if existing and fingerprint(existing) == fingerprint(url) and self.sockets.get(target):
                        pong = await self.sockets[target].ping()
                        await asyncio.wait_for(pong, 5)
                    else:
                        async with DirectConnect(url, proxy=None, open_timeout=7, close_timeout=1,
                                                 max_size=65536, max_queue=4, **options) as ws:
                            pong = await ws.ping()
                            await asyncio.wait_for(pong, 3)
                return {"ok": True, "message": "WebSocket 握手与心跳通过；业务工具是否可用请在智能体侧验证"}
            except Exception:
                return {"ok": False, "message": "连接未通过，请检查接入点有效期、授权和网络后重试"}
