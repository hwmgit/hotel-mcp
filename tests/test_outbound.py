import asyncio
import json
import pytest
from websockets.asyncio.server import serve
from hotel_mcp.outbound import validate_endpoint, run_connector
from hotel_mcp.server import Endpoint, create_app
from hotel_mcp.store import HotelStore, Principal


@pytest.mark.parametrize('url', ['https://example.com/mcp/', 'ws://example.com/mcp/',
                                'wss://user:secret@example.com/mcp/', 'wss://example.com/call/',
                                'wss://example.com/mcp/#secret'])
def test_reject_unsafe_or_wrong_direction_url(url):
    with pytest.raises(ValueError):
        validate_endpoint(url)


def test_configured_identity_cannot_share_endpoint(tmp_path):
    rows = [{'id': key, 'role': 'frontdesk', 'hotel': key, 'token': key * 40,
             'mcp_endpoint': 'wss://example.com/mcp/?token=private'} for key in ['a', 'b']]
    with pytest.raises(ValueError, match='exactly one'):
        create_app({'principals': rows}, tmp_path / 'test.db')


@pytest.mark.asyncio
async def test_outbound_real_handshake_role_tools_events_and_reconnect(tmp_path):
    store = HotelStore(tmp_path / 'test.db')
    async def publish():
        await endpoint.notify()
    endpoint = Endpoint(store, Principal('a', 'frontdesk', principal_id='desk'), 'x' * 40, publish)
    connections = asyncio.Queue()
    async def peer(ws):
        await connections.put(ws)
        await ws.wait_closed()
    async with serve(peer, '127.0.0.1', 0) as server:
        url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp/'
        status = {}
        task = asyncio.create_task(run_connector(endpoint, validate_endpoint(url), status, retry_seconds=.01))
        try:
            ws = await asyncio.wait_for(connections.get(), 2)
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': 0, 'method': 'initialize', 'params': {
                'protocolVersion': '2024-11-05', 'capabilities': {}, 'clientInfo': {'name': 'test', 'version': '1'}}}))
            initialized = json.loads(await asyncio.wait_for(ws.recv(), 2))
            assert initialized['result']['capabilities']['resources']['subscribe']
            await ws.send(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list', 'params': {}}))
            names = [x['name'] for x in json.loads(await ws.recv())['result']['tools']]
            assert 'hotel_accept_request' in names and 'hotel_submit_request' not in names
            await ws.send(json.dumps({'jsonrpc': '2.0', 'id': 2, 'method': 'resources/list', 'params': {}}))
            assert json.loads(await ws.recv())['result']['resources'][0]['uri'] == 'business://events'
            await ws.close()
            second = await asyncio.wait_for(connections.get(), 3)
            assert second is not ws
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert status['state'] == 'stopped'
