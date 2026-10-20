import argparse
import json
import os
import secrets
import tempfile
from pathlib import Path
import uvicorn
from .server import create_app
from .store import HotelStore, Principal


def main():
    parser = argparse.ArgumentParser(description="本地酒店 MCP 参考服务")
    parser.add_argument("--config", help="默认 data/config.json；绑定入住时必须显式指定")
    parser.add_argument("--database", help="默认 data/hotel.sqlite3；维护操作必须显式指定")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8120)
    maintenance = parser.add_mutually_exclusive_group()
    maintenance.add_argument("--init", action="store_true", help="生成本地独立凭证，不覆盖已有配置")
    maintenance.add_argument("--set-mcp-endpoint", action="store_true", help="为指定身份配置云端 MCP 接入点；隐藏输入地址，留空停用；重启生效")
    maintenance.add_argument("--migrate", action="store_true", help="显式迁移指定数据库；需先备份并停止其写入服务")
    maintenance.add_argument("--bind-stay", metavar="STAY_ID", help="将客房配置绑定有效入住并轮换凭据；服务重启后生效")
    maintenance.add_argument("--init-sales-demo", metavar="NEW_DIRECTORY", help="在全新目录生成独立销售演示库和凭据，不启动服务")
    maintenance.add_argument("--init-demo", metavar="NEW_DIRECTORY", help="在全新目录生成五类业务、前台网页和客房身份的完整演示数据")
    parser.add_argument("--demo-end-date", help="销售演示数据结束日期 YYYY-MM-DD，默认北京时间今天，包含最近7天")
    parser.add_argument("--principal-id", help="--bind-stay 要更新的客房配置 ID")
    args = parser.parse_args()
    if args.demo_end_date and not (args.init_sales_demo or args.init_demo):
        parser.error("--demo-end-date 仅用于演示初始化")
    if args.init_sales_demo or args.init_demo:
        if args.config or args.database or args.principal_id:
            parser.error("销售演示只能生成全新目录，不接受现有 --config/--database/--principal-id")
        from .demo import init_sales_demo, init_full_demo
        try:
            summary = (init_full_demo if args.init_demo else init_sales_demo)(args.init_demo or args.init_sales_demo, args.demo_end_date)
        except (ValueError, FileExistsError) as error:
            parser.error(str(error))
        print(json.dumps(summary, ensure_ascii=False))
        return
    path = Path(args.config or "data/config.json")
    if args.set_mcp_endpoint:
        if not args.config or not args.principal_id:
            parser.error("配置 MCP 接入点需要显式 --config 和 --principal-id")
        import getpass
        from .outbound import validate_endpoint
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = [row for row in config["principals"] if row["id"] == args.principal_id]
        if len(rows) != 1:
            parser.error("找不到唯一业务身份")
        url = getpass.getpass("粘贴智控台 MCP 接入点地址（隐藏输入；留空停用）：").strip()
        if url:
            validate_endpoint(url)
            if any(row is not rows[0] and row.get("mcp_endpoint") == url for row in config["principals"]):
                parser.error("同一 MCP 接入点不能绑定多个业务身份")
            rows[0]["mcp_endpoint"] = url
        else:
            rows[0].pop("mcp_endpoint", None)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(config, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        print("MCP connection configuration saved. Restart this instance to apply; address not printed.")
        return
    if args.migrate or args.bind_stay:
        if not args.database:
            parser.error("维护操作必须显式指定 --database，不使用默认运行数据库")
        if args.database != "mysql" and not Path(args.database).is_file():
            parser.error("维护操作要求数据库文件已经存在")
    if args.migrate:
        HotelStore(args.database, migrate=True)
        print("Database migration completed.")
        return
    if args.bind_stay:
        if not args.config:
            parser.error("--bind-stay 必须显式指定 --config")
        if not args.principal_id:
            parser.error("--bind-stay 需要 --principal-id")
        config = json.loads(path.read_text(encoding="utf-8-sig"))
        rows = [row for row in config["principals"] if row["id"] == args.principal_id and row["role"] == "room"]
        if len(rows) != 1:
            parser.error("找不到唯一客房身份")
        row = rows[0]
        store = HotelStore(args.database)
        store.authorize(Principal(row["hotel"], "room", row["room"], args.bind_stay, row["id"]))
        row["stay_id"], row["token"] = args.bind_stay, secrets.token_urlsafe(32)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(config, stream, ensure_ascii=False, indent=2)
            os.replace(temporary, path)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
        print("Stay bound and credential rotated in private config. Restart required; token not printed.")
        return
    if args.init:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [{"id": "room-101", "hotel": "demo-hotel", "role": "room", "room": "101"},
                {"id": "frontdesk", "hotel": "demo-hotel", "role": "frontdesk"}]
        for row in rows:
            row["token"] = secrets.token_urlsafe(32)
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"rooms": [{"hotel": "demo-hotel", "room": "101"}], "principals": rows},
                      stream, ensure_ascii=False, indent=2)
        print(f"Created private configuration: {path.resolve()}")
        return
    app = create_app(json.loads(path.read_text(encoding="utf-8-sig")), args.database or "data/hotel.sqlite3", config_path=path)
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)


if __name__ == "__main__":
    main()
