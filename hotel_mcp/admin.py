"""Same-origin UI adapter; delegates every business operation to MCP's dispatcher."""
import hmac
import json
import sqlite3
from pathlib import Path

from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from .domain import BusinessError, fail
from .toolkit import READ_TOOLS, tools_for


def admin_routes(endpoints, config):
    by_id = {e.principal.principal_id: e for e in endpoints}
    static = Path(__file__).parent / "web"

    def identity(request):
        endpoint = by_id.get(request.path_params["principal_id"])
        if not endpoint or not hmac.compare_digest(request.headers.get("authorization", "").encode(),
                                                   ("Bearer " + endpoint.token).encode()):
            fail("UNAUTHORIZED", "入口编号或访问凭据不正确")
        endpoint.store.authorize(endpoint.principal)
        return endpoint

    async def api(request):
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        try:
            endpoint = identity(request)
            if request.method == "GET":
                p = endpoint.principal
                return JSONResponse({"principal": {"id": p.principal_id, "role": p.role, "hotel": p.hotel, "room": p.room},
                                     "tools": [t.name for t in tools_for(p)]}, headers=headers)
            raw = await request.body()
            if len(raw) > 65536:
                fail("VALIDATION_ERROR", "请求内容过大")
            try:
                arguments = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                fail("VALIDATION_ERROR", "请求须为 JSON")
            name = request.path_params["tool"]
            result = endpoint.execute(name, arguments)
            if name not in READ_TOOLS:
                await endpoint.publish()
            return JSONResponse(result, headers=headers)
        except BusinessError as exc:
            status = {"UNAUTHORIZED": 401, "FORBIDDEN": 403, "NOT_FOUND": 404,
                      "VERSION_CONFLICT": 409, "RESOURCE_UNAVAILABLE": 409, "IDEMPOTENCY_CONFLICT": 409}.get(exc.code, 400)
            return JSONResponse(exc.payload(), status_code=status, headers=headers)
        except sqlite3.OperationalError:
            return JSONResponse(BusinessError("TEMPORARY_UNAVAILABLE", "暂时无法处理，请使用原操作重试", True).payload(),
                                status_code=503, headers=headers)

    async def page(request):
        return FileResponse(static / "index.html", headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY",
                                                            "X-Content-Type-Options": "nosniff"})

    async def public_config(request):
        # Only public analytics identifiers; never serialize the private config or any principal.
        analytics = config.get("analytics", {})
        return JSONResponse({"analytics": {k: analytics.get(k) for k in
                             ("environment", "enabled", "script_url", "website_id", "production_domain")}},
                            headers={"Cache-Control": "no-store"})

    return [Route("/", lambda request: RedirectResponse("/admin/")), Route("/admin", page), Route("/admin/", page),
            Route("/admin/config", public_config), Mount("/admin/assets", StaticFiles(directory=static)),
            Route("/api/{principal_id}/session", api, methods=["GET"]),
            Route("/api/{principal_id}/tools/{tool}", api, methods=["POST"])]
