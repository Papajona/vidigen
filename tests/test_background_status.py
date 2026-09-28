import asyncio
import os
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

os.environ.setdefault('VIDIGEN_GATEWAY_TOKEN', 'test-token')

from gateway import providers
from gateway import server


@pytest.mark.parametrize('provider_status, raw, expected_status, error', [
    ('processing', {}, 'processing', None),
    ('failed', {'error': 'Prediction failed'}, 'error', 'Prediction failed'),
    ('canceled', None, 'error', 'Background removal failed.'),
])
def test_background_removal_status_for_owned_job(
    provider_status, raw, expected_status, error
):
    job_id = "bg-test-123"
    server.BACKGROUND_JOB_CACHE[job_id] = {
        "user_id": "local-gateway",
        "kind": "image",
        "provider_job_id": "provider-123",
        "created_at": time.time(),
    }

    async def call():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            return await client.get(
                f"/api/remove-background/status/{job_id}",
                headers={"Authorization": "Bearer test-token"},
            )

    try:
        fake = providers.GenerationResult(
            "replicate-rembg",
            "provider-123",
            provider_status,
            None,
            raw if isinstance(raw, dict) else {},
        )
        with patch.object(
            server.persistence, "enabled", return_value=False
        ), patch.object(
            providers, "background_status", new=AsyncMock(return_value=fake)
        ) as poll:
            response = asyncio.run(call())

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == expected_status
        assert body["job_id"] == job_id
        poll.assert_awaited_once_with("provider-123", "image")

        if error:
            assert body["error"] == error
            assert job_id not in server.BACKGROUND_JOB_CACHE
    finally:
        server.BACKGROUND_JOB_CACHE.pop(job_id, None)
