"""每个凭证一个独立 MCP 会话域，阻止跨客房/酒店复用 session ID。"""
import asyncio
import hmac
import json
import re
import weakref
from contextlib import AsyncExitStack, asynccontextmanager
from urllib.parse import parse_qs, urlsplit

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .store import HotelStore, Principal

INBOX = "business://events"


def tool(name, description, properties=None, required=None):
    return types.Tool(name=name, description=description, inputSchema={"type": "object",
                      "properties": properties or {}, "required": required or [], "additionalProperties": False})


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
        self.register()

    def register(self):
        @self.server.list_tools()
        async def list_tools():
            items = [tool("hotel_list_requests", "查询当前凭证授权范围内的酒店工单。", {"after": {"type": "string"}})]
            if self.principal.role == "room":
                items.append(tool("hotel_submit_request", "提交当前客房请求；同一次请求和重试必须复用 request_key。房号由服务端身份确定。",
                                  {"request_key": {"type": "string", "minLength": 1, "maxLength": 80},
                                   "description": {"type": "string", "minLength": 1, "maxLength": 300}},
                                  ["request_key", "description"]))
            else:
                for action in ("accept", "complete"):
                    items.append(tool("hotel_" + action + "_request", "仅在前台人员明确确认后" + ("受理" if action == "accept" else "完成") + "工单。",
                                      {"request_id": {"type": "string"}, "note": {"type": "string", "maxLength": 160}}, ["request_id"]))
            return items

        @self.server.call_tool()
        async def call_tool(name, arguments):
            # Unknown tools must be rejected even though the SDK skips their schema validation.
            if name not in {item.name for item in await list_tools()}:
                raise ValueError("tool not authorized")
            if name == "hotel_list_requests":
                return self.store.list_requests(self.principal, arguments.get("after", ""))
            if name == "hotel_submit_request":
                result = self.store.submit(self.principal, arguments["request_key"], arguments["description"])
            else:
                result = self.store.transition(self.principal, arguments["request_id"],
                                               "accepted" if name == "hotel_accept_request" else "completed", arguments.get("note", ""))
            await self.publish()
            return result

        @self.server.list_resources()
        async def list_resources():
            return [types.Resource(uri=AnyUrl(INBOX), name="business_events", mimeType="application/json",
                                   description="按凭证隔离的持久化事件；使用 ?after=序号读取下一页。")]

        @self.server.read_resource()
        async def read_resource(uri):
            parsed = urlsplit(str(uri))
            if parsed.scheme != "business" or parsed.netloc != "events" or parsed.path not in ("", "/") or parsed.fragment:
                raise ValueError("unknown resource")
            params = parse_qs(parsed.query, strict_parsing=True)
            if set(params) - {"after"} or len(params.get("after", ["0"])) != 1:
                raise ValueError("invalid cursor")
            data = self.store.inbox(self.principal, int(params.get("after", ["0"])[0]))
            return [ReadResourceContents(content=json.dumps(data, ensure_ascii=False), mime_type="application/json")]

        @self.server.subscribe_resource()
        async def subscribe(uri):
            if str(uri).rstrip("/") != INBOX or len(self.subscribers) >= 128:
                raise ValueError("resource unavailable")
            self.subscribers.add(self.server.request_context.session)

        @self.server.unsubscribe_resource()
        async def unsubscribe(uri):
            self.subscribers.discard(self.server.request_context.session)

    async def notify(self):
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
        await asyncio.gather(*(endpoint.notify() for endpoint in endpoints))

    routes, ids, tokens = [], set(), set()
    for row in config["principals"]:
        key, token = row["id"], row["token"]
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", key) or key in ids or token in tokens or len(token) < 32:
            raise ValueError("unique endpoint IDs and unique tokens of at least 32 characters required")
        ids.add(key); tokens.add(token)
        endpoint = Endpoint(store, Principal(row["hotel"], row["role"], row.get("room", "")), token, publish)
        endpoints.append(endpoint)
        routes.append(Route("/" + key + "/mcp", endpoint, methods=["GET", "POST", "DELETE"]))

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as stack:
            for endpoint in endpoints:
                await stack.enter_async_context(endpoint.manager.run())
            yield

    async def health(request):
        return JSONResponse({"status": "ok", "service": "hotel-mcp", "version": "1.0.0"})

    return Starlette(routes=[Route("/health", health), *routes], lifespan=lifespan)
