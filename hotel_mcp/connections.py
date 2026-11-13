"""Private, single-process MCP connection settings and hot connection lifecycle."""
import asyncio
import hashlib
import ipaddress
import json
import os
import socket
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from .domain import Principal, fail, text
from .agent_profiles import AGENT_TYPES, public_types
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
    def __init__(self, endpoints, config, config_path=None, allow_local=False, *, store=None, endpoint_factory=None):
        self.store, self.endpoint_factory, self.publish_endpoints = store, endpoint_factory, endpoints
        self.endpoints = {e.principal.principal_id: e for e in endpoints}
        self.path = Path(config_path).with_name("mcp-connections.json") if config_path else None
        self.allow_local = allow_local
        self.lock = asyncio.Lock()
        self.active = False
        self.tasks, self.sockets, self.status = {}, {}, {}
        self.records = {p["id"]: {"url": p.get("mcp_endpoint", ""), "version": 0, "managed": False,
                                   "present": bool(p.get("mcp_endpoint")), "enabled": bool(p.get("mcp_endpoint")), "name": ""}
                        for p in config["principals"]}
        self.receipts = []
        if self.path and self.path.exists():
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if saved.get("schema_version") not in (1, 2):
                raise ValueError("Unsupported private MCP settings version")
            self.receipts = saved.get("receipts", [])
            for key, value in saved["connections"].items():
                if value.get("agent"):
                    if key in self.endpoints or not self.endpoint_factory or not key.startswith("agent-"):
                        raise ValueError("Invalid dynamic agent identity in private settings")
                    endpoint = self.endpoint_factory(self._principal(value["agent"], key))
                    self.endpoints[key] = endpoint
                    self.records[key] = value
                    if value.get("present", True):
                        self.publish_endpoints.append(endpoint)
                if key in self.records:
                    self.records[key] = {"present": bool(value["url"]), "enabled": bool(value["url"]), "name": "",
                                         **value, "managed": True}
        urls = set()
        for key, record in self.records.items():
            if record["url"]:
                validate_endpoint(record["url"])
                mark = fingerprint(record["url"])
                if mark in urls:
                    raise ValueError("Each MCP endpoint must belong to exactly one business identity")
                urls.add(mark)
                self.status[key] = {"state": "stopped"}

    @staticmethod
    def can_manage(actor, target):
        if actor.account_id:
            if actor.account_role not in ("super_admin", "store_admin") or not actor.hotel:
                return False
            if target.role == "manager":
                return (actor.hotel in target.allowed_hotels if actor.account_role == "super_admin"
                        else set(target.allowed_hotels) == {actor.hotel})
            return target.hotel == actor.hotel and target.role in ("room", "frontdesk")
        if actor.role == "manager":
            return target.role == "manager" and target.principal_id == actor.principal_id
        return actor.role == "frontdesk" and target.hotel == actor.hotel and target.role in ("room", "frontdesk")

    def authorize(self, actor, target=None):
        if actor.role not in ("frontdesk", "manager"):
            fail("FORBIDDEN", "仅前台或经营管理身份可管理 AI 接入配置")
        if not self.path:
            fail("FORBIDDEN", "当前实例未配置可持久化的配置文件路径")
        if target is not None:
            endpoint = self.endpoints.get(target)
            if not endpoint or not self.can_manage(actor, endpoint.principal):
                fail("NOT_FOUND", "找不到可管理的业务身份")
            if actor.account_id:
                endpoint.store.authorize(actor)

    def snapshot(self, actor, *, configured_only=False):
        self.authorize(actor)
        rows, available = [], []
        for key, endpoint in self.endpoints.items():
            p = endpoint.principal
            if not self.can_manage(actor, p):
                continue
            record = self.records[key]
            default_name = p.room + " 号客房" if p.role == "room" else "经营销售查询" if p.role == "manager" else "前台服务"
            agent_type = record.get("agent", {}).get("type", "sales" if p.role == "manager" else p.role)
            identity = {"id": key, "role": p.role, "room": p.room, "allowed_hotels": list(p.allowed_hotels),
                        "agent_type": agent_type, "type_label": AGENT_TYPES[agent_type]["label"],
                        "dynamic": bool(record.get("agent")), "stay_id": p.stay_id,
                        "version": record["version"], "name": record["name"] or default_name}
            if not record["present"]:
                if not record.get("agent"):
                    available.append(identity)
                if configured_only:
                    continue
            parsed = urlsplit(record["url"]) if record["url"] else None
            # Paths and query parameters may both contain credentials.
            masked = f"{parsed.scheme}://{parsed.hostname}/…/mcp/（凭据已隐藏）" if parsed else ""
            rows.append({**identity, "present": record["present"], "enabled": record["enabled"],
                         "configured": bool(parsed), "masked_endpoint": masked,
                         "state": self.status.get(key, {}).get("state", "stopped") if record["enabled"] else "disabled"})
        return {"connections": rows, "available_identities": available, "agent_types": public_types()}

    @staticmethod
    def _principal(definition, key):
        profile = AGENT_TYPES[definition["type"]]
        return Principal(definition["hotel"] if profile["role"] != "manager" else "", profile["role"],
                         room=definition.get("room", ""), stay_id=definition.get("stay_id", ""), principal_id=key,
                         allowed_hotels=(definition["hotel"],) if profile["role"] == "manager" else (),
                         agent_type=definition["type"], request_types=profile["request_types"])

    def _agent_admin(self, actor):
        self.authorize(actor)
        if not actor.account_id or actor.account_role not in ("super_admin", "store_admin") or not actor.hotel:
            fail("FORBIDDEN", "仅管理员选定具体门店后可管理智能体")
        self.store.authorize(actor)

    def _definition(self, actor, kind, stay_id):
        if not isinstance(kind, str) or kind not in AGENT_TYPES or not isinstance(stay_id, str) or len(stay_id) > 80:
            fail("VALIDATION_ERROR", "请选择有效的智能体类型和入住记录")
        definition = {"type": kind, "hotel": actor.hotel, "room": "", "stay_id": ""}
        if kind == "room":
            with self.store.connect() as db:
                stay = db.execute("SELECT s.id,s.room FROM stays s JOIN rooms r ON r.hotel=s.hotel AND r.room=s.room "
                                  "WHERE s.id=? AND s.hotel=? AND s.status='active' AND r.enabled=1",
                                  (stay_id, actor.hotel)).fetchone()
            if not stay:
                fail("VALIDATION_ERROR", "请为客房助手选择本店有效入住")
            definition.update(stay_id=stay["id"], room=stay["room"])
        elif stay_id:
            fail("VALIDATION_ERROR", "仅客房助手需要绑定入住")
        return definition

    def options(self, actor, offset=0):
        self._agent_admin(actor)
        with self.store.connect() as db:
            rows = db.execute("SELECT s.id,s.room,s.check_in_at FROM stays s JOIN rooms r ON r.hotel=s.hotel AND r.room=s.room "
                              "WHERE s.hotel=? AND s.status='active' AND r.enabled=1 "
                              "ORDER BY s.room,s.id LIMIT 51 OFFSET ?", (actor.hotel, offset)).fetchall()
        return {"agent_types": public_types(), "stays": [dict(r) for r in rows[:50]],
                "next_offset": offset + 50 if len(rows) > 50 else None}

    async def create_agent(self, actor, body):
        self._agent_admin(actor)
        if (not isinstance(body, dict) or set(body) != {"name", "agent_type", "stay_id", "endpoint", "enabled", "request_key"}
                or type(body["enabled"]) is not bool or not isinstance(body["endpoint"], str) or len(body["endpoint"]) > 4096):
            fail("VALIDATION_ERROR", "请填写智能体名称、类型、接入点和启用状态")
        name = text(body["name"], "智能体名称", 80)
        text(body["request_key"], "操作编号", 80)
        url = body["endpoint"].strip()
        digest = hashlib.sha256(json.dumps([actor.hotel, "create_agent", body], sort_keys=True).encode()).hexdigest()
        async with self.lock:
            self._agent_admin(actor)
            for receipt in self.receipts:
                if (receipt["actor"], receipt["key"]) == (actor.principal_id, body["request_key"]):
                    if receipt["digest"] != digest:
                        fail("IDEMPOTENCY_CONFLICT", "同一操作编号不能用于不同配置")
                    return {**receipt["result"], "replayed": True}
            await destination(url, self.allow_local)
            key = "agent-" + uuid.uuid4().hex
            self._unique(key, url)
            self._agent_admin(actor)
            definition = self._definition(actor, body["agent_type"], body["stay_id"])
            endpoint = self.endpoint_factory(self._principal(definition, key))
            record = {"name": name, "url": url, "enabled": body["enabled"], "present": True,
                      "managed": True, "version": 1, "agent": definition}
            result = {"id": key, "version": 1, "enabled": body["enabled"], "replayed": False}
            records = {**self.records, key: record}
            receipts = [*self.receipts, {"actor": actor.principal_id, "key": body["request_key"], "digest": digest,
                                       "version": 1, "result": result}]
            try:
                self._persist(records, receipts)
            except OSError:
                fail("TEMPORARY_UNAVAILABLE", "配置保存失败，原连接保持不变，请重试")
            self.records, self.receipts = records, receipts
            self.endpoints[key] = endpoint
            self.publish_endpoints.append(endpoint)
            await self._launch(key)
            return result

    async def _launch(self, key):
        record = self.records[key]
        if not self.active or not record["url"] or not record["enabled"] or not record["present"]:
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
                json.dump({"schema_version": 2,
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
        if self.records[target].get("agent"):
            fail("VALIDATION_ERROR", "请使用智能体编辑或启停操作")
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
            records = {**self.records, target: {**previous, "url": url, "version": version, "managed": True,
                                               "present": bool(url), "enabled": bool(url)}}
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

    async def manage(self, actor, target, action, body):
        """Explicit connection lifecycle; disabling never discards the address.

        Deleted entries keep a private version tombstone, preventing base config
        resurrection or a stale browser from silently recreating a connection.
        """
        self.authorize(actor, target)
        if action == "create" and self.records[target].get("agent"):
            fail("VALIDATION_ERROR", "请通过新增智能体创建独立接入")
        fields = {"expected_version", "request_key"}
        if action == "create":
            fields |= {"endpoint", "name", "enabled"}
        elif action == "update":
            fields |= {"endpoint", "name"}
            if self.records[target].get("agent"):
                fields |= {"agent_type", "stay_id"}
        elif action not in ("enable", "disable", "delete"):
            fail("VALIDATION_ERROR", "不支持的接入操作")
        if (not isinstance(body, dict) or set(body) != fields
                or type(body["expected_version"]) is not int or body["expected_version"] < 0):
            fail("VALIDATION_ERROR", "请提供完整的接入配置、版本和操作编号")
        text(body["request_key"], "操作编号", 80)
        if action in ("create", "update"):
            name = text(body["name"], "接入名称", 80)
            if not isinstance(body["endpoint"], str) or len(body["endpoint"]) > 4096:
                fail("VALIDATION_ERROR", "接入地址格式不正确")
        if action == "create" and type(body["enabled"]) is not bool:
            fail("VALIDATION_ERROR", "启用状态须为布尔值")
        digest = hashlib.sha256(json.dumps([target, action, body], sort_keys=True).encode()).hexdigest()
        async with self.lock:
            self.authorize(actor, target)
            for receipt in self.receipts:
                if (receipt["actor"], receipt["key"]) == (actor.principal_id, body["request_key"]):
                    if receipt["digest"] != digest:
                        fail("IDEMPOTENCY_CONFLICT", "同一操作编号不能用于不同配置")
                    return {**receipt["result"], "replayed": True}
            previous = self.records[target]
            if previous["version"] != body["expected_version"]:
                fail("VERSION_CONFLICT", "配置已更新，请刷新后重试")
            if action == "create" and previous["present"]:
                fail("VERSION_CONFLICT", "此业务身份已有接入配置，请编辑现有配置")
            if action != "create" and not previous["present"]:
                fail("NOT_FOUND", "接入配置已删除，请刷新列表")
            changed = {**previous, "managed": True, "version": previous["version"] + 1}
            replacement = None
            if action in ("create", "update"):
                url = body["endpoint"].strip()
                if action == "create" and not url:
                    fail("VALIDATION_ERROR", "请填写新的接入地址")
                changed.update(name=name, url=url or previous["url"], present=True)
                if action == "create":
                    changed["enabled"] = body["enabled"]
                # Validate newly supplied addresses even for disabled drafts.
                if url:
                    await destination(url, self.allow_local)
                    self._unique(target, url)
                if previous.get("agent"):
                    self._agent_admin(actor)
                    changed["agent"] = self._definition(actor, body["agent_type"], body["stay_id"])
                    if changed["agent"] != previous["agent"]:
                        replacement = self.endpoint_factory(self._principal(changed["agent"], target))
            elif action == "delete":
                changed.update(url="", name="", present=False, enabled=False)
            else:
                changed["enabled"] = action == "enable"
            if action == "enable":
                await destination(changed["url"], self.allow_local)
                self._unique(target, changed["url"])
                self.endpoints[target].store.authorize(self.endpoints[target].principal)
            self.authorize(actor, target)
            result = {"saved": True, "version": changed["version"], "enabled": changed["enabled"],
                      "deleted": not changed["present"], "replayed": False}
            records = {**self.records, target: changed}
            receipts = [*self.receipts, {"actor": actor.principal_id, "key": body["request_key"],
                                       "digest": digest, "version": changed["version"], "result": result}]
            try:
                self._persist(records, receipts)
            except OSError:
                fail("TEMPORARY_UNAVAILABLE", "配置保存失败，原连接保持不变，请重试")
            self.records, self.receipts = records, receipts
            if replacement or (previous["url"], previous["enabled"], previous["present"]) != (changed["url"], changed["enabled"], changed["present"]):
                await self._stop(target)
                if replacement or (action == "delete" and previous.get("agent")):
                    old_endpoint = self.endpoints[target]
                    if old_endpoint in self.publish_endpoints:
                        self.publish_endpoints.remove(old_endpoint)
                    if replacement:
                        self.endpoints[target] = replacement
                        self.publish_endpoints.append(replacement)
                await self._launch(target)
            return result

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
