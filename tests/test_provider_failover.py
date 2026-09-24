import asyncio
import os
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault('VIDIGEN_GATEWAY_TOKEN', 'test-token')

from gateway import server
from gateway.providers import GenerationResult, ProviderError


@dataclass
class _FakeProvider:
    name: str
    error: Exception | None = None

    async def submit(self, request):
        if self.error:
            raise self.error
        return GenerationResult(self.name, f'{self.name}-job-1', 'starting', None, {})

    async def status(self, job_id, status_url=None):
        return GenerationResult(self.name, job_id, 'processing', None, {})


async def _call(payload):
    transport = httpx.ASGITransport(app=server.app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        return await client.post(
            '/api/generate',
            json=payload,
            headers={'Authorization': 'Bearer test-token'},
        )


def test_generate_fails_over_to_next_configured_provider():
    fake_replicate = _FakeProvider('replicate', ProviderError('Replicate unavailable'))
    fake_seedance = _FakeProvider('seedance')

    with patch.dict(
        os.environ,
        {
            'REPLICATE_API_TOKEN': 'set',
            'REPLICATE_MODEL': 'test-model',
            'SEEDANCE_API_URL': 'https://seedance.example/jobs',
            'SEEDANCE_API_TOKEN': 'set',
            'RUNWAY_API_URL': '',
            'RUNWAY_API_TOKEN': '',
            'VIDIGEN_PROVIDER_PRIORITY': 'replicate,seedance,runway',
        },
        clear=False,
    ), patch.dict(
        server.PROVIDERS,
        {'replicate': fake_replicate, 'seedance': fake_seedance},
        clear=False,
    ), patch.object(
        server.persistence, 'enabled', return_value=False
    ), patch.object(
        server, '_bill_generation', new=AsyncMock(return_value={'charged': 0})
    ):
        response = asyncio.run(_call({'prompt': 'a cinematic city at sunset'}))

    assert response.status_code == 200
    body = response.json()
    assert body['provider'] == 'seedance'
    assert body['fallbackUsed'] is True
    assert body['providerAttempts'] == ['replicate', 'seedance']
    assert body['externalJobId'] == 'seedance-job-1'


def test_status_fails_over_when_accepted_provider_later_fails():
    failed = _FakeProvider('replicate')
    fallback = _FakeProvider('seedance')

    async def failed_status(job_id, status_url=None):
        return GenerationResult('replicate', job_id, 'failed', None, {'error': 'provider job failed'})

    failed.status = failed_status

    job_id = 'status-fallback-job'
    original = server.JOB_CACHE.get(job_id)
    server.JOB_CACHE[job_id] = {
        'provider': 'replicate',
        'external_id': 'replicate-job-1',
        'user_id': 'local-gateway',
        'request': {
            'prompt': 'a cinematic city at sunset',
            'mode': 'Text → Video',
            '_billing': {'charged': 0},
            '_provider_candidates': ['replicate', 'seedance', 'runway'],
            '_provider_attempts': ['replicate'],
        },
    }

    async def call():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            return await client.get(
                f'/api/status/{job_id}',
                headers={'Authorization': 'Bearer test-token'},
            )

    try:
        with patch.dict(server.PROVIDERS, {'replicate': failed, 'seedance': fallback}, clear=False),              patch.object(server.persistence, 'enabled', return_value=False):
            response = asyncio.run(call())
        assert response.status_code == 200
        body = response.json()
        assert body['status'] == 'running'
        assert body['provider'] == 'seedance'
        assert body['fallbackUsed'] is True
        assert body['providerAttempts'] == ['replicate', 'seedance']
    finally:
        if original is None:
            server.JOB_CACHE.pop(job_id, None)
        else:
            server.JOB_CACHE[job_id] = original
