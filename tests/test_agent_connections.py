"""Independent agent lifecycle, live transport, capabilities and tenant boundaries."""
import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from hotel_mcp.accounts import Accounts, COOKIE
from hotel_mcp.connections import Connections
from hotel_mcp.domain import BusinessError
from hotel_mcp.server import Endpoint, create_app
from hotel_mcp.toolkit import tools_for
from tests.test_accounts import INITIAL, create, login_account
from tests.test_connections import setting_server
from tests.test_services import services
from tests.test_business import assert_error


def admin(http, store):
    accounts = Accounts(store)
    accounts.bootstrap('agent-admin', INITIAL)
    root, token = login_account(accounts, 'agent-admin')
    http.cookies.set(COOKIE, token)
    http.headers.update({'X-Requested-With': 'hotel-admin', 'X-CSRF-Token': accounts.csrf(token),
                         'X-Hotel-ID': 'demo-central'})
    return accounts, root


def draft(url, key='new-agent', kind='dining', **values):
    return {'name': '餐饮助手', 'agent_type': kind, 'stay_id': '', 'endpoint': url,
            'enabled': True, 'request_key': key, **values}


async def manage(http, key, action, version, **values):
    return await http.post(f'/workspace/connections/{key}/manage/{action}', json={
        'expected_version': version, 'request_key': f'{key}-{action}-{version}', **values})


async def rpc(ws, number, method, params=None):
    await ws.send(json.dumps({'jsonrpc': '2.0', 'id': number, 'method': method,
                             **({'params': params} if params is not None else {})}))
    return json.loads(await asyncio.wait_for(ws.recv(), 3))['result']


@pytest.mark.asyncio
async def test_independent_agent_live_lifecycle_restore_notifications_and_delete(setting_server):
    http, config, path, app, store = setting_server
    accounts, root = admin(http, store)
    queue = asyncio.Queue()
    async def peer(ws):
        await queue.put(ws)
        await ws.wait_closed()
    async with serve(peer, '127.0.0.1', 0) as server:
        url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp/?token=private-test'
        payload = draft(url)
        created = await http.post('/workspace/connections', json=payload)
        assert created.status_code == 200, created.text
        key = created.json()['id']
        connection = app.state.connections
        first = await asyncio.wait_for(queue.get(), 3)
        await rpc(first, 1, 'initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                                         'clientInfo': {'name': 'agent-test', 'version': '1'}})
        await first.send(json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}))
        names = {t['name'] for t in (await rpc(first, 2, 'tools/list'))['tools']}
        assert 'hotel_process_request' in names and 'hotel_end_stay' not in names
        await rpc(first, 3, 'resources/subscribe', {'uri': 'business://events'})
        room = connection.endpoints['room-demo-central-101']
        room.execute('hotel_submit_meal_order', {'items': [{'item_id': store.services.menu(room.principal)['items'][0]['id'], 'quantity': 1}], 'request_key': 'agent-event'})
        await room.publish()
        notification = json.loads(await asyncio.wait_for(first.recv(), 3))
        assert notification['method'] == 'notifications/resources/updated'
        assert (await http.post('/workspace/connections', json=payload)).json()['replayed']
        listing = await http.get('/workspace/connections')
        assert listing.json()['connections'][0]['name'] == '餐饮助手' and 'private-test' not in listing.text
        assert (await http.post(f'/workspace/connections/{key}/test', json={})).json()['ok']
        assert queue.empty()
        assert (await manage(http, key, 'disable', 1)).status_code == 200
        await asyncio.wait_for(first.wait_closed(), 3)
        assert connection.records[key]['url'] == url and key not in connection.tasks
        restored = create_app(config, store.path, config_path=path, allow_local_mcp=True).state.connections
        assert not restored.records[key]['enabled'] and restored.records[key]['url'] == url
        assert restored.endpoints[key].principal.agent_type == 'dining'
        assert (await manage(http, key, 'enable', 2)).status_code == 200
        second = await asyncio.wait_for(queue.get(), 3)
        old_endpoint = connection.endpoints[key]
        changed = await manage(http, key, 'update', 3, name='维修助手', endpoint='', agent_type='repair', stay_id='')
        assert changed.status_code == 200, changed.text
        await asyncio.wait_for(second.wait_closed(), 3)
        third = await asyncio.wait_for(queue.get(), 3)
        assert old_endpoint not in connection.publish_endpoints
        assert connection.endpoints[key].principal.request_types == ('repair',)
        with store.connect() as db:
            before = tuple(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('requests', 'accounts', 'stays'))
        deleted = await manage(http, key, 'delete', 4)
        assert deleted.status_code == 200
        await asyncio.wait_for(third.wait_closed(), 3)
        assert connection.endpoints[key] not in connection.publish_endpoints
        assert (await manage(http, key, 'delete', 4)).json()['replayed']
        assert not (await http.get('/workspace/connections')).json()['connections']
        restored = create_app(config, store.path, config_path=path, allow_local_mcp=True).state.connections
        assert not restored.records[key]['present'] and not restored.records[key]['url']
        with store.connect() as db:
            assert before == tuple(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in ('requests', 'accounts', 'stays'))


@pytest.mark.asyncio
async def test_agent_validation_permissions_conflicts_and_failed_persistence(setting_server, monkeypatch):
    http, config, path, app, store = setting_server
    accounts, root = admin(http, store)
    connection = app.state.connections
    options = (await http.get('/workspace/connections/options')).json()
    assert {r['room'] for r in options['stays']} == {'101', '102'}
    async with serve(lambda ws: ws.wait_closed(), '127.0.0.1', 0) as server:
        url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp/'
        for changes in ({'agent_type': 'unknown'}, {'agent_type': 'room'}, {'agent_type': 'room', 'stay_id': 'foreign'}, {'stay_id': options['stays'][0]['id']}, {'name': ''}):
            result = await http.post('/workspace/connections', json=draft(url, **changes))
            assert result.status_code == 400, result.text
        room = await http.post('/workspace/connections', json=draft(url+'?room=1', 'room', 'room', stay_id=options['stays'][0]['id'], enabled=False))
        assert room.status_code == 200, room.text
        same = await asyncio.gather(*[http.post('/workspace/connections', json=draft(url, f'parallel-{n}', enabled=False)) for n in range(2)])
        assert sorted(r.status_code for r in same) == [200, 400]
        key = next(r.json()['id'] for r in same if r.status_code == 200)
        other = await http.post('/workspace/connections', json=draft(url+'?other=1', 'other', enabled=False))
        assert other.status_code == 200
        assert (await http.post('/workspace/connections', json=draft(url+'?other=2', 'other', enabled=False))).status_code == 409
        assert (await manage(http, key, 'enable', 0)).status_code == 409
        assert (await http.post(f'/workspace/connections/{key}', json={'endpoint': '', 'expected_version': 1, 'request_key': 'old-api'})).status_code == 400
        http.headers['X-Hotel-ID'] = 'demo-airport'
        assert not (await http.get('/workspace/connections')).json()['connections']
        assert (await manage(http, key, 'delete', 1)).status_code == 404
        http.headers['X-Hotel-ID'] = 'demo-central'
        original = connection._persist
        def broken(*args):
            raise OSError('sensitive-path')
        monkeypatch.setattr(connection, '_persist', broken)
        failed = await manage(http, key, 'delete', 1)
        assert failed.status_code == 503 and 'sensitive-path' not in failed.text
        assert connection.records[key]['present'] and connection.records[key]['version'] == 1
        monkeypatch.setattr(connection, '_persist', original)
        create(accounts, root, 'agent-manager', 'store_admin')
        create(accounts, root, 'agent-staff', 'frontdesk')
        create(accounts, root, 'agent-guest', 'customer')
        for username in ('agent-manager', 'agent-staff', 'agent-guest'):
            actor, token = login_account(accounts, username)
            http.cookies.set(COOKIE, token)
            http.headers['X-CSRF-Token'] = accounts.csrf(token)
            status = 200 if username == 'agent-manager' else 403
            assert (await http.get('/workspace/connections')).status_code == status
            if status == 403:
                assert (await http.post('/workspace/connections', json=draft(url+'?staff=1', username))).status_code == 403
                assert (await manage(http, key, 'delete', 1)).status_code == 403


def assert_profile_scope(store, room, desk):
    async def publish():
        pass
    rows = store.query_requests(desk, limit=50)['requests']
    for kind, allowed in [('dining', {'meal'}), ('repair', {'repair'}), ('booking', {'gym_booking', 'meeting_booking'})]:
        principal = Connections._principal({'hotel': desk.hotel, 'type': kind}, 'agent-scope')
        endpoint = Endpoint(store, principal, '', publish)
        names = {t.name for t in tools_for(principal)}
        assert 'hotel_process_request' in names and 'hotel_query_sales' not in names and 'hotel_end_stay' not in names
        result = endpoint.execute('hotel_query_requests', {'limit': 1})
        seen = result['requests']
        while result['next_cursor']:
            result = endpoint.execute('hotel_query_requests', {'limit': 1, 'cursor': result['next_cursor']})
            seen += result['requests']
        assert seen and {r['request_type'] for r in seen} <= allowed
        foreign = next(r for r in rows if r['request_type'] not in allowed)
        for name, args in [('hotel_get_request', {'request_id': foreign['id']}),
                           ('hotel_list_request_actions', {'request_id': foreign['id']}),
                           ('hotel_process_request', {'request_id': foreign['id'], 'action': 'accept', 'note': 'test', 'expected_version': foreign['version'], 'request_key': kind})]:
            assert_error('NOT_FOUND', endpoint.execute, name, args)
        assert_error('FORBIDDEN', endpoint.execute, 'hotel_end_stay', {})
        events = store.inbox(principal)['events']
        assert events and {e['request_type'] for e in events} <= allowed
        assert store.latest_event_seq(principal) == max(e['seq'] for e in events)
    # A profile change must also revoke access to cached write responses.
    repair = Endpoint(store, Connections._principal({'hotel': desk.hotel, 'type': 'repair'}, 'agent-replay'), '', publish)
    meal = Endpoint(store, Connections._principal({'hotel': desk.hotel, 'type': 'dining'}, 'agent-replay'), '', publish)
    row = next(r for r in rows if r['request_type'] == 'repair')
    args = {'request_id': row['id'], 'action': 'accept', 'note': 'test', 'expected_version': row['version'], 'request_key': 'old-scope'}
    repair.execute('hotel_process_request', args)
    assert_error('NOT_FOUND', meal.execute, 'hotel_process_request', args)


def test_agent_profiles_scope_tickets_events_and_replayed_responses(services):
    store, room, desk, _, _ = services
    assert_profile_scope(store, room, desk)


@pytest.mark.asyncio
async def test_old_settings_import_delete_tombstone_and_stale_room(setting_server):
    http, config, path, app, store = setting_server
    accounts, root = admin(http, store)
    connection = app.state.connections
    async with serve(lambda ws: ws.wait_closed(), '127.0.0.1', 0) as server:
        url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/mcp/'
        key = 'room-demo-central-101'
        connection.path.write_text(json.dumps({'schema_version': 1, 'connections': {key: {'url': url, 'version': 2}}, 'receipts': []}))
        restored = create_app(config, store.path, config_path=path, allow_local_mcp=True).state.connections
        assert restored.records[key]['present'] and restored.records[key]['enabled']
        actor = accounts.context(root, 'demo-central', '')[0]
        await restored.manage(actor, key, 'delete', {'expected_version': 2, 'request_key': 'remove-old'})
        next(p for p in config['principals'] if p['id'] == key)['mcp_endpoint'] = url
        again = create_app(config, store.path, config_path=path, allow_local_mcp=True).state.connections
        assert not again.records[key]['present'] and not again.records[key]['url']
        room = connection.endpoints[key].principal
        made = await connection.create_agent(actor, draft(url, kind='room', stay_id=room.stay_id, enabled=False))
        store.end_stay(actor, room.stay_id, 1, 'test checkout', 'agent-checkout')
        with pytest.raises(BusinessError) as error:
            await connection.manage(actor, made['id'], 'enable', {'expected_version': 1, 'request_key': 'inactive'})
        assert error.value.code == 'STAY_INACTIVE'
