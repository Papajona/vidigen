import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from httpx import ASGITransport, AsyncClient

os.environ.setdefault('VIDIGEN_GATEWAY_TOKEN', 'test-token')
os.environ.setdefault('VIDIGEN_ALLOWED_ORIGINS', 'http://localhost:5173')
os.environ.setdefault('WORKFLOW_FILE', '/tmp/vidigen-test-missing-workflow.json')

from gateway.server import app

@pytest.mark.anyio
async def test_auth_and_health():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as c:
        r = await c.get('/health')
        assert r.status_code == 401
        r = await c.get('/health', headers={'Authorization':'Bearer test-token'})
        assert r.status_code == 200
        assert r.json()['ok'] is True

@pytest.mark.anyio
async def test_analyzer_fallback_and_validation():
    transport = ASGITransport(app=app)
    headers={'Authorization':'Bearer test-token'}
    async with AsyncClient(transport=transport, base_url='http://test') as c:
        r = await c.post('/api/analyze', headers=headers, json={'prompt':'Create a cinematic product video in a luxury studio'})
        assert r.status_code == 200
        data=r.json()
        assert data.get('type')
        r = await c.post('/api/generate', headers=headers, json={'prompt':'x','ratio':'bad'})
        assert r.status_code == 422


def test_critical_media_routes_are_registered():
    paths = {getattr(route, 'path', '') for route in app.routes}
    assert '/api/r2-presign' in paths
    assert '/api/r2-register' in paths
    assert '/api/remove-background' in paths
    assert '/api/remove-background/status/{job_id}' in paths
    assert '/api/auto-reframe' in paths
    assert '/api/storage/me' in paths


@pytest.mark.anyio
async def test_r2_upload_guards_require_signed_media():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as c:
        r = await c.post('/api/r2-presign', json={'object_key':'x','content_type':'application/octet-stream'})
        assert r.status_code == 401
        r = await c.get('/api/storage/me')
        assert r.status_code == 401
