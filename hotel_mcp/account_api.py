"""Same-origin cookie API. Business calls still use the existing dispatcher."""
import asyncio
import hmac
import json
import sqlite3
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.routing import Route

from .accounts import Accounts, COOKIE, SESSION_SECONDS, ADMIN_ROLES
from .domain import BusinessError, fail
from .toolkit import READ_TOOLS

HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def account_routes(store, publish, connections):
    accounts = Accounts(store)

    async def body(request):
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            fail("VALIDATION_ERROR", "请求须使用 JSON")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                fail("VALIDATION_ERROR", "请求内容过大")
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            fail("VALIDATION_ERROR", "请求须为有效 JSON")
        if not isinstance(value, dict):
            fail("VALIDATION_ERROR", "请求须为对象")
        return value

    def origin(request):
        if request.headers.get("sec-fetch-site") == "cross-site":
            fail("FORBIDDEN", "请从工作台页面操作")
        supplied = request.headers.get("origin")
        if supplied:
            parsed = urlsplit(supplied)
            if parsed.netloc != request.headers.get("host") or parsed.scheme not in ("http", "https"):
                fail("FORBIDDEN", "请从工作台页面操作")
            if request.url.hostname not in ("localhost", "127.0.0.1", "::1", "testserver") and parsed.scheme != "https":
                fail("FORBIDDEN", "请使用 HTTPS 工作台")
        if request.headers.get("x-requested-with") != "hotel-admin":
            fail("FORBIDDEN", "请从工作台页面操作")

    def offset(request):
        try:
            value = int(request.query_params.get("offset", "0"))
            if not 0 <= value <= 1000000:
                raise ValueError()
            return value
        except ValueError:
            fail("VALIDATION_ERROR", "无效的分页参数")

    async def handle(request):
        try:
            operation = request.scope["endpoint_operation"]
            if request.method == "POST":
                origin(request)
            if operation == "login":
                data = await body(request)
                if set(data) != {"username", "password"}:
                    fail("VALIDATION_ERROR", "请输入账号和密码")
                token = await asyncio.to_thread(accounts.login, data["username"], data["password"],
                                                request.client.host if request.client else "unknown")
                response = JSONResponse({"authenticated": True}, headers=HEADERS)
                response.set_cookie(COOKIE, token, max_age=SESSION_SECONDS, httponly=True, samesite="strict",
                                    secure=request.url.scheme == "https" or request.url.hostname not in
                                    ("localhost", "127.0.0.1", "::1", "testserver"), path="/")
                return response
            token = request.cookies.get(COOKIE, "")
            actor = await asyncio.to_thread(accounts.authenticate, token)
            if request.method == "POST" and not hmac.compare_digest(
                    request.headers.get("x-csrf-token", "").encode(), accounts.csrf(token).encode()):
                fail("FORBIDDEN", "页面验证已失效，请刷新后重试")
            hotel, stay = request.headers.get("x-hotel-id", ""), request.headers.get("x-stay-id", "")
            if actor["must_change_password"] and operation not in ("session", "logout", "password"):
                fail("PASSWORD_CHANGE_REQUIRED", "请先修改初始密码")
            if operation == "session":
                result = await asyncio.to_thread(accounts.session, actor, hotel, stay, connections=connections.path is not None)
                result["csrf_token"] = accounts.csrf(token)
            elif operation == "logout":
                await asyncio.to_thread(accounts.logout, token)
                result = {"logged_out": True}
            elif operation == "password":
                result = await asyncio.to_thread(accounts.change_password, actor, await body(request))
            elif operation == "accounts":
                if request.method == "GET":
                    result = await asyncio.to_thread(accounts.list_accounts, actor, hotel, offset(request), request.query_params.get("q", ""))
                else:
                    result = await asyncio.to_thread(accounts.mutate, actor, await body(request))
            elif operation == "account":
                result = await asyncio.to_thread(accounts.mutate, actor, await body(request), request.path_params["account_id"], "update")
            elif operation in ("reset_password", "bind_stay"):
                result = await asyncio.to_thread(accounts.mutate, actor, await body(request), request.path_params["account_id"], operation)
            elif operation == "stays":
                result = await asyncio.to_thread(accounts.stay_options, actor, request.path_params["account_id"], offset(request))
            elif operation == "tool":
                name = request.path_params["tool"]
                result = await asyncio.to_thread(accounts.execute, actor, hotel, stay, name, await body(request))
                if name not in READ_TOOLS:
                    await publish()
            else:
                if actor["role"] not in ADMIN_ROLES:
                    fail("FORBIDDEN", "仅管理员可维护 AI 接入配置")
                principal, _, _, _ = await asyncio.to_thread(accounts.context, actor, hotel, stay)
                if not principal or not principal.hotel:
                    fail("FORBIDDEN", "请先选择具体门店")
                target = request.path_params.get("target_id")
                connections.authorize(principal, target)
                if operation == "connection_options":
                    result = connections.options(principal, offset(request))
                elif operation == "connection_create":
                    result = await asyncio.shield(connections.create_agent(principal, await body(request)))
                elif request.method == "GET":
                    result = connections.snapshot(principal, configured_only=True)
                elif operation == "connection_test":
                    result = await connections.test(principal, target, await body(request))
                elif operation == "connection_manage":
                    result = await asyncio.shield(connections.manage(principal, target, request.path_params["action"], await body(request)))
                else:
                    result = await asyncio.shield(connections.save(principal, target, await body(request)))
            response = JSONResponse(result, headers=HEADERS)
            if operation in ("logout", "password"):
                response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
            return response
        except BusinessError as exc:
            code = {"UNAUTHORIZED": 401, "FORBIDDEN": 403, "PASSWORD_CHANGE_REQUIRED": 403, "NOT_FOUND": 404,
                    "VERSION_CONFLICT": 409, "IDEMPOTENCY_CONFLICT": 409, "RESOURCE_UNAVAILABLE": 409,
                    "USERNAME_EXISTS": 409, "RATE_LIMITED": 429, "TEMPORARY_UNAVAILABLE": 503}.get(exc.code, 400)
            return JSONResponse(exc.payload(), status_code=code, headers=HEADERS)
        except sqlite3.Error:
            return JSONResponse(BusinessError("TEMPORARY_UNAVAILABLE", "暂时无法处理，请使用原操作重试", True).payload(),
                                status_code=503, headers=HEADERS)

    def route(path, operation, methods):
        async def endpoint(request):
            request.scope["endpoint_operation"] = operation
            return await handle(request)
        return Route("/workspace/" + path, endpoint, methods=methods)

    return [route("login", "login", ["POST"]), route("session", "session", ["GET"]),
            route("logout", "logout", ["POST"]), route("password", "password", ["POST"]),
            route("accounts", "accounts", ["GET", "POST"]),
            route("accounts/{account_id}", "account", ["POST"]),
            route("accounts/{account_id}/reset_password", "reset_password", ["POST"]),
            route("accounts/{account_id}/stays", "stays", ["GET"]),
            route("accounts/{account_id}/bind_stay", "bind_stay", ["POST"]),
            route("tools/{tool}", "tool", ["POST"]),
            route("connections", "connections", ["GET"]),
            route("connections", "connection_create", ["POST"]),
            route("connections/options", "connection_options", ["GET"]),
            route("connections/{target_id}/test", "connection_test", ["POST"]),
            route("connections/{target_id}/manage/{action}", "connection_manage", ["POST"]),
            route("connections/{target_id}", "connection_save", ["POST"])]
