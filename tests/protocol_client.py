"""Test-only MCP resource reader; business package never imports the platform."""
import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from pydantic import AnyUrl

def http_client_factory(headers=None, timeout=None, auth=None):
    return httpx.AsyncClient(headers=headers, timeout=timeout or 30, auth=auth, trust_env=False)

class CursorStore:
    def __init__(self, path):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS cursors (consumer TEXT PRIMARY KEY, seq INTEGER)")
    def read(self, consumer):
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute("SELECT seq FROM cursors WHERE consumer=?", (consumer,)).fetchone()
            return row[0] if row else 0
    def ack(self, consumer, seq):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT OR REPLACE INTO cursors VALUES (?,?)", (consumer, seq))

async def consume(config, cursors, consumer, deliver, poll_seconds=5):
    wake = asyncio.Event()
    async def changed(message):
        wake.set()
    async with streamablehttp_client(**config, httpx_client_factory=http_client_factory) as (read, write, _):
        async with ClientSession(read, write, message_handler=changed) as session:
            await session.initialize()
            await session.subscribe_resource(AnyUrl("business://events"))
            while True:
                wake.clear()
                result = await session.read_resource(AnyUrl(f"business://events?after={cursors.read(consumer)}"))
                page = json.loads(result.contents[0].text)
                for event in page["events"]:
                    await deliver({**event, "event_id": hashlib.sha256(f"{consumer}:{event['seq']}".encode()).hexdigest()})
                    cursors.ack(consumer, event["seq"])
                if page.get("has_more"):
                    continue
                try:
                    await asyncio.wait_for(wake.wait(), poll_seconds)
                except asyncio.TimeoutError:
                    pass
