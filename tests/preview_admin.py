"""Disposable loopback preview, with test-only credentials and an explicit stop file."""
import argparse
import asyncio
import json
import socket
import tempfile
from pathlib import Path

import uvicorn

from hotel_mcp.demo import init_full_demo
from hotel_mcp.server import create_app


async def preview(stop_file):
    with tempfile.TemporaryDirectory(prefix="hotel-ui-preview-") as directory:
        root = Path(directory) / "demo"
        init_full_demo(root, "2026-10-18")
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        for row in config["principals"]:
            row["token"] = "test-only-" + row["id"] + "-" + "x" * 32
        app = create_app(config, root / "hotel.sqlite3")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        print(f"Preview: http://127.0.0.1:{sock.getsockname()[1]}/admin/", flush=True)
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False, timeout_graceful_shutdown=2))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(1800):
                if stop_file.exists() or task.done():
                    break
                await asyncio.sleep(.5)
        finally:
            server.should_exit = True
            await task
            sock.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stop-file", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(preview(args.stop_file))
