import asyncio
import os
import httpx

os.environ.setdefault('VIDIGEN_GATEWAY_TOKEN', 'test-token')

from gateway.server import app


async def call(method, path, payload=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        return await client.request(method, path, json=payload, headers={'Authorization': 'Bearer test-token'})


def test_edit_plan_trim():
    r = asyncio.run(call('POST', '/api/edit-plan', {
        'command': 'trim the clip to 3s',
        'clips': [{'id': 'c1', 'title': 'Shot 1', 'duration': 5, 'trimStart': 0, 'trimEnd': 5}],
        'ratio': '16:9'
    }))
    assert r.status_code == 200
    assert r.json()['operations'][0]['trimEnd'] == 3.0


def test_export_plan_is_explicit():
    r = asyncio.run(call('POST', '/api/export-plan', {
        'ratio': '9:16',
        'clips': [{'id': 'c1', 'duration': 5, 'trimStart': 1, 'trimEnd': 4}],
        'captionCount': 4,
        'hasAudio': True
    }))
    assert r.status_code == 200
    body = r.json()
    assert body['render']['duration'] == 3.0
    assert 'Connect a native FFmpeg/MediaCodec worker' in body['message']


def test_render_requires_durable_http_sources():
    r = asyncio.run(call('POST', '/api/render', {
        'ratio': '16:9',
        'clips': [{'uri': 'blob:not-server-readable', 'trimStartMs': 0}]
    }))
    assert r.status_code in (400, 501, 503)
