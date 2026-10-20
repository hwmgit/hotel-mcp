"""Business-owned outbound MCP connections; no hardware-platform dependency."""
import asyncio
import ipaddress
import logging
import random
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import anyio
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidHandshake

LOG = logging.getLogger(__name__)


class DirectConnect(connect):
    def process_redirect(self, exc):
        return InvalidHandshake("Redirects are not supported")


def validate_endpoint(url):
    try:
        p = urlsplit(url)
        if (len(url) > 4096 or any(ord(c) < 32 or ord(c) == 127 for c in url)
                or not p.hostname or p.username or p.password or p.fragment
                or not p.path.rstrip('/').endswith('/mcp') or p.scheme not in ('ws', 'wss')):
            raise ValueError()
        if p.scheme == 'ws' and p.hostname != 'localhost' and not ipaddress.ip_address(p.hostname).is_loopback:
            raise ValueError()
        _ = p.port
    except (ValueError, TypeError, AttributeError):
        raise ValueError('MCP endpoint must be wss://…/mcp/?token=… (ws only on loopback)') from None
    return url


@asynccontextmanager
async def server_streams(url, options=None, on_socket=None):
    async with DirectConnect(url, proxy=None, open_timeout=10, close_timeout=2,
                       max_size=1024 * 1024, max_queue=32, **(options or {})) as ws:
        if on_socket:
            on_socket(ws)
        incoming, reader = anyio.create_memory_object_stream(32)
        writer, outgoing = anyio.create_memory_object_stream(32)
        async def receive():
            async with incoming:
                async for raw in ws:
                    await incoming.send(SessionMessage(JSONRPCMessage.model_validate_json(raw)))
            raise ConnectionError('MCP disconnected')
        async def send():
            async with outgoing:
                async for item in outgoing:
                    await ws.send(item.message.model_dump_json(by_alias=True, exclude_none=True))
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(receive)
            tasks.start_soon(send)
            try:
                yield reader, writer
            finally:
                tasks.cancel_scope.cancel()


async def run_connector(endpoint, url, status, retry_seconds=1, resolve=None, on_socket=None):
    """Reconnect transport only; never replay an in-flight business command."""
    delay = retry_seconds
    while True:
        status['state'] = 'connecting'
        try:
            options = await resolve(url) if resolve else None
            async with server_streams(url, options, on_socket) as (reader, writer):
                status['state'] = 'connected'
                delay = retry_seconds
                await endpoint.server.run(reader, writer, endpoint.server.create_initialization_options())
        except asyncio.CancelledError:
            status['state'] = 'stopped'
            raise
        except Exception:
            # Exception text may contain the credential-bearing URL.
            status['state'] = 'retrying'
            LOG.warning('MCP connection unavailable: principal=%s', endpoint.principal.principal_id)
        finally:
            if on_socket:
                on_socket(None)
        await asyncio.sleep(delay + random.uniform(0, delay / 4))
        delay = min(delay * 2, 30)
