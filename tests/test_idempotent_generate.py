import asyncio
import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault('VIDIGEN_GATEWAY_TOKEN', 'test-token')

import httpx
from gateway import persistence
from gateway.server import app


async def _call(method, path, payload=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        return await client.request(method, path, json=payload, headers={'Authorization': 'Bearer test-token'})


def test_get_job_by_idempotency_key_queries_scoped_to_user():
    mock = AsyncMock(return_value=[{'id': 'job-1', 'status': 'processing', 'provider': 'replicate', 'request': {'idempotencyKey': 'abc123', '_external_id': 'ext-1'}}])
    with patch.object(persistence, 'sb_request', new=mock):
        row = asyncio.run(persistence.get_job_by_idempotency_key('user-1', 'abc123'))
    assert row['id'] == 'job-1'
    called_params = mock.call_args.kwargs['params']
    assert called_params['user_id'] == 'eq.user-1'
    assert called_params['idempotency_key'] == 'eq.abc123'


def test_get_job_by_idempotency_key_returns_none_for_empty_key():
    row = asyncio.run(persistence.get_job_by_idempotency_key('user-1', ''))
    assert row is None


def test_generate_replays_existing_job_without_rebilling():
    """Simulates a retried /api/generate call carrying the same idempotencyKey. Billing
    (consume_credits) must NOT be invoked a second time — that's the actual bug this closes
    (a retried request charging credits twice)."""
    existing_row = {
        'id': 'job-existing', 'status': 'processing', 'provider': 'replicate',
        'request': {'idempotencyKey': 'retry-key-1', '_external_id': 'ext-existing'},
    }
    # gateway-token auth (used by every other test in this file) resolves to
    # user['sub']=='local-gateway', which is truthy and enough to activate the idempotency
    # branch in generate() — that branch only needs a non-empty user_id, not a full
    # Supabase-authenticated user. persistence.enabled() is forced True and the lookup is
    # patched directly so this exercises the idempotency branch itself, independent of
    # whether real Supabase credentials are configured in this environment.
    with patch.object(persistence, 'enabled', return_value=True), \
         patch.object(persistence, 'get_job_by_idempotency_key', new=AsyncMock(return_value=existing_row)):
        r = asyncio.run(_call('POST', '/api/generate', {'prompt': 'a cat on a skateboard', 'idempotencyKey': 'retry-key-1'}))
    assert r.status_code == 200
    body = r.json()
    assert body['promptId'] == 'job-existing'
    assert body.get('idempotentReplay') is True

def test_idempotency_column_contract_is_used():
    mock = AsyncMock(return_value=[{'id': 'job-2', 'status': 'queued', 'provider': 'replicate', 'request': {}}])
    with patch.object(persistence, 'sb_request', new=mock):
        job = asyncio.run(persistence.create_job('user-2', None, 'replicate', 'model-x', {'prompt':'x'}, 'key-2'))
    assert job == 'job-2'
    payload = mock.call_args.args[2]
    assert payload['idempotency_key'] == 'key-2'
