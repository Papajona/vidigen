import asyncio
import os
import httpx

os.environ.setdefault('VIDIGEN_GATEWAY_TOKEN', 'test-token')

from gateway.server import app, Generate, _provider_candidates


async def call(method, path, payload=None):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
        return await client.request(method, path, json=payload, headers={'Authorization': 'Bearer test-token'})


def test_generate_accepts_provider_model_and_source_contracts():
    req = Generate(prompt='cinematic product ad', mode='Image → Video', model='replicate', sourceUrl='https://cdn.example/source.jpg', sourceType='image')
    assert req.model == 'replicate'
    assert req.sourceType == 'image'


def test_generate_rejects_missing_transform_source():
    req = Generate(prompt='animate this image', mode='Image → Video', model='replicate')
    try:
        _provider_candidates('replicate', req)
    except Exception as exc:
        assert getattr(exc, 'status_code', None) == 400
    else:
        raise AssertionError('Image → Video must require a source image')


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


def test_render_clip_accepts_editor_properties():
    from gateway.server import RenderClip
    clip = RenderClip(
        uri='https://cdn.example/clip.mp4',
        speed=1.5,
        volume=0.75,
        brightness=110,
        contrast=105,
        saturation=120,
        blur=2,
        rotation=15,
        scale=90,
        opacity=80,
        overlay={'text':'Launch now', 'size':36, 'x':50, 'y':80, 'bold':True},
    )
    assert clip.speed == 1.5
    assert clip.overlay.text == 'Launch now'


def test_render_filter_preserves_editor_controls():
    from gateway.server import RenderClip, _clip_video_filter
    clip = RenderClip(
        uri='https://cdn.example/clip.mp4', speed=1.5, scale=90, opacity=80,
        overlay={'text':'Launch now', 'size':36, 'x':50, 'y':80, 'bold':True},
    )
    vf = _clip_video_filter('16:9', clip)
    assert 'setpts=PTS/1.500000' in vf
    assert 'lutrgb=' in vf
    assert 'drawtext=' in vf
    assert 'fontsize=36.0' in vf


def test_render_requires_durable_http_sources():
    r = asyncio.run(call('POST', '/api/render', {
        'ratio': '16:9',
        'clips': [{'uri': 'blob:not-server-readable', 'trimStartMs': 0}]
    }))
    assert r.status_code in (400, 501, 503)

def test_gemini_route_fails_closed_when_not_configured(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    r = asyncio.run(call('POST', '/api/gemini/analyze', {'prompt': 'cinematic product ad'}))
    assert r.status_code == 503

def test_healthz_is_public_liveness_probe():
    transport = httpx.ASGITransport(app=app)
    async def check():
        async with httpx.AsyncClient(transport=transport, base_url='http://test') as client:
            return await client.get('/healthz')
    r = asyncio.run(check())
    assert r.status_code == 200
    assert r.json() == {'ok': True}


def test_feature_health_probe_is_nonsecret(monkeypatch):
    monkeypatch.setenv('R2_ENDPOINT','x')
    monkeypatch.setenv('R2_ACCESS_KEY_ID','x')
    monkeypatch.setenv('R2_SECRET_ACCESS_KEY','x')
    monkeypatch.setenv('R2_BUCKET','x')
    monkeypatch.setenv('R2_PUBLIC_BASE_URL','https://cdn.example')
    monkeypatch.delenv('REPLICATE_API_TOKEN', raising=False)
    r = asyncio.run(call('GET', '/api/features/health'))
    assert r.status_code == 200
    body = r.json()
    assert body['gateway'] is True
    assert body['google_pay'] is False
    assert 'providers' in body and 'configured' in body['providers']
    assert 'REPLICATE_API_TOKEN' not in str(body)
