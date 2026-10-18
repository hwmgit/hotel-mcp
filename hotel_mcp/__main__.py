import argparse
import json
import secrets
from pathlib import Path
import uvicorn
from .server import create_app


def main():
    parser = argparse.ArgumentParser(description="本地酒店 MCP 参考服务")
    parser.add_argument("--config", default="data/config.json")
    parser.add_argument("--database", default="data/hotel.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8120)
    parser.add_argument("--init", action="store_true", help="生成本地独立凭证，不覆盖已有配置")
    args = parser.parse_args()
    path = Path(args.config)
    if args.init:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"id": "room-101", "hotel": "demo-hotel", "role": "room", "room": "101"},
                {"id": "frontdesk", "hotel": "demo-hotel", "role": "frontdesk"}]
        for row in rows:
            row["token"] = secrets.token_urlsafe(32)
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"principals": rows}, stream, ensure_ascii=False, indent=2)
        print(f"Created private configuration: {path.resolve()}")
        return
    app = create_app(json.loads(path.read_text(encoding="utf-8-sig")), args.database)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
