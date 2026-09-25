import asyncio
from unittest.mock import AsyncMock, patch

import httpx

from gateway import providers
from gateway import server


def test_background_removal_status_returns_processing_for_owned_job():
    job_id = "bg-test-123"
    server.BACKGROUND_JOB_CACHE[job_id] = {
        "user_id": "local-gateway",
        "kind": "image",
        "created_at": __import__("time").time(),
    }

    async def call():
        transport = httpx.ASGITransport(app=server.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get(
                f"/api/remove-background/status/{job_id}",
                headers={"Authorization": "Bearer test-token"},
            )

    try:
        fake = providers.GenerationResult(
            "replicate-rembg",
            job_id,
            "processing",
            None,
            {},
        )
        with patch.object(providers, "background_status", new=AsyncMock(return_value=fake)):
            response = asyncio.run(call())
        assert response.status_code == 200
        assert response.json()["status"] == "processing"
        assert response.json()["job_id"] == job_id
    finally:
        server.BACKGROUND_JOB_CACHE.pop(job_id, None)
