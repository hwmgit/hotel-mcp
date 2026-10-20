"""每个凭证一个独立 MCP 会话域，阻止跨客房/酒店复用 session ID。"""
import asyncio
import hmac
import json
import re
import sqlite3
import weakref
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import parse_qs, urlsplit

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyUrl
from jsonschema import ValidationError, validate
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .store import HotelStore, Principal
from .domain import BusinessError, fail
from .toolkit import READ_TOOLS, dispatch, tools_for

INBOX = "business://events"


class SubscribableServer(Server):
    def get_capabilities(self, notification_options, experimental_capabilities):
        capabilities = super().get_capabilities(notification_options, experimental_capabilities)
        # SDK 1.22 defaults this to False even when subscribe handlers are installed.
        if capabilities.resources:
            capabilities.resources.subscribe = True
        return capabilities


class Endpoint:
    def __init__(self, store, principal, token, publish):
        self.store, self.principal, self.token, self.publish = store, principal, token, publish
        self.subscribers = weakref.WeakSet()
        self.server = SubscribableServer("whitecat-hotel", version="1.0.0")
        self.manager = StreamableHTTPSessionManager(self.server)
        try:
            self.notified_seq = store.latest_event_seq(principal)
        except BusinessError:
            self.notified_seq = 0
        self.register()

    def execute(self, name, arguments):
        """Shared authorization, schema and dispatch for MCP and the staff UI."""
        self.store.authorize(self.principal)
        schemas = {item.name: item.inputSchema for item in tools_for(self.principal)}
        if name not in schemas:
            fail("FORBIDDEN", "此身份无权调用该工具")
        try:
            validate(arguments, schemas[name])
        except ValidationError:
            fail("VALIDATION_ERROR", "参数不符合工具 schema；请检查必填项、类型和范围")
        return dispatch(self.store, self.principal, name, arguments)

    def register(self):
        @self.server.list_tools()
        async def list_tools():
            self.store.authorize(self.principal)
            return tools_for(self.principal)

        @self.server.call_tool(validate_input=False)
        async def call_tool(name, arguments):
            try:
                result = self.execute(name, arguments)
            except BusinessError as error:
                return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=json.dumps(error.payload(), ensure_ascii=False))])
            except sqlite3.OperationalError:
                error = BusinessError("TEMPORARY_UNAVAILABLE", "数据库暂不可用，请使用原 request_key 重试", True)
                return types.CallToolResult(isError=True, content=[types.TextContent(type="text", text=json.dumps(error.payload(), ensure_ascii=False))])
            if name not in READ_TOOLS:
                await self.publish()
            return result

        @self.server.list_resources()
        async def list_resources():
            self.store.authorize(self.principal)
            if self.principal.role == "manager":
                return []
            return [types.Resource(uri=AnyUrl(INBOX), name="business_events", mimeType="application/json",
                                   description="按凭证隔离的持久化事件；使用 ?after=序号读取下一页。")]

        @self.server.read_resource()
        async def read_resource(uri):
            parsed = urlsplit(str(uri))
            if parsed.scheme != "business" or parsed.netloc != "events" or parsed.path not in ("", "/") or parsed.fragment:
                raise ValueError("unknown resource")
            params = parse_qs(parsed.query, strict_parsing=True, keep_blank_values=True)
            if set(params) - {"after"} or len(params.get("after", ["0"])) != 1:
                raise ValueError("invalid cursor")
            data = self.store.inbox(self.principal, int(params.get("after", ["0"])[0]))
            return [ReadResourceContents(content=json.dumps(data, ensure_ascii=False), mime_type="application/json")]

        @self.server.subscribe_resource()
        async def subscribe(uri):
            self.store.authorize(self.principal)
            if self.principal.role == "manager":
                fail("FORBIDDEN", "销售查询身份不订阅住客工单事件")
            if str(uri).rstrip("/") != INBOX or len(self.subscribers) >= 128:
                raise ValueError("resource unavailable")
            self.subscribers.add(self.server.request_context.session)

        @self.server.unsubscribe_resource()
        async def unsubscribe(uri):
            self.subscribers.discard(self.server.request_context.session)

    async def notify(self):
        try:
            latest = self.store.latest_event_seq(self.principal)
        except BusinessError:
            self.subscribers.clear()
            return
        if latest <= self.notified_seq:
            return
        self.notified_seq = latest
        async def send(session):
            try:
                await asyncio.wait_for(session.send_resource_updated(AnyUrl(INBOX)), 1)
            except Exception:
                self.subscribers.discard(session)
        await asyncio.gather(*(send(session) for session in list(self.subscribers)))

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        supplied = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(supplied, ("Bearer " + self.token).encode()):
            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self.manager.handle_request(scope, receive, send)


def create_app(config, database):
    store = HotelStore(database)
    endpoints = []

    async def publish():
        # Business transaction has committed; notification failure cannot turn it into a failed write.
        await asyncio.gather(*(endpoint.notify() for endpoint in endpoints), return_exceptions=True)

    store.configure_rooms(config.get("rooms", []))
    if "store_aliases" in config:
        store.entities.configure_aliases(config["store_aliases"])

    routes, ids, tokens = [], set(), set()
    outbound, connection_status = [], {}
    for row in config["principals"]:
        key, token = row["id"], row["token"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key) or key in ids or token in tokens or len(token) < 32:
            raise ValueError("unique endpoint IDs and unique tokens of at least 32 characters required")
        ids.add(key); tokens.add(token)
        allowed = row.get("allowed_hotels", [])
        if not isinstance(allowed, list):
            raise ValueError("allowed_hotels must be a list")
        endpoint = Endpoint(store, Principal(row.get("hotel", ""), row["role"], row.get("room", ""),
                                             row.get("stay_id", ""), key, tuple(allowed)), token, publish)
        endpoints.append(endpoint)
        if row.get("mcp_endpoint"):
            from .outbound import validate_endpoint
            url = validate_endpoint(row["mcp_endpoint"])
            if any(existing == url for _, existing in outbound):
                raise ValueError("Each MCP endpoint must belong to exactly one business identity")
            outbound.append((endpoint, url))
            connection_status[key] = {"state": "stopped"}
        routes.append(Route("/" + key + "/mcp", endpoint, methods=["GET", "POST", "DELETE"]))

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for endpoint in endpoints:
                await stack.enter_async_context(endpoint.manager.run())
            from .outbound import run_connector
            tasks = [asyncio.create_task(run_connector(endpoint, url, connection_status[endpoint.principal.principal_id]))
                     for endpoint, url in outbound]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def health(request):
        if store.is_mysql:
            try:
                with store.connect() as db:
                    db.execute("SELECT 1").fetchone()
            except sqlite3.Error:
                return JSONResponse({"status": "unavailable", "service": "hotel-mcp"}, status_code=503)
        return JSONResponse({"status": "ok", "service": "hotel-mcp", "version": "1.0.0",
                             "mcp_connections": connection_status})

    from .admin import admin_routes
    return Starlette(routes=[Route("/health", health), *routes, *admin_routes(endpoints, config)], lifespan=lifespan)
