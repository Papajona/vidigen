"""Server-side, provider-neutral AI adapters. Secrets stay on the gateway."""
from dataclasses import dataclass
from typing import Any
import os, httpx, time, asyncio
@dataclass
class GenerationResult:
    provider:str; job_id:str; status:str; output_url:str|None=None; raw:dict[str,Any]|None=None
class ProviderError(RuntimeError): pass
def _output_url(data: dict) -> str | None:
    out=data.get('output')
    if isinstance(out,str): return out
    if isinstance(out,list) and out and isinstance(out[0],str): return out[0]
    for key in ('output_url','outputUrl','url','video_url','videoUrl','result_url','resultUrl'):
        value=data.get(key)
        if isinstance(value,str) and value: return value
    return None

def _status_url(data: dict) -> str | None:
    urls=data.get('urls') or {}
    for key in ('get','status'):
        value=urls.get(key) if isinstance(urls,dict) else None
        if isinstance(value,str) and value: return value
    for key in ('status_url','statusUrl','status_endpoint','statusEndpoint'):
        value=data.get(key)
        if isinstance(value,str) and value: return value
    return None

class ReplicateProvider:
    name='replicate'
    def __init__(self): self.token=os.getenv('REPLICATE_API_TOKEN',''); self.model=os.getenv('REPLICATE_MODEL','')
    async def submit(self,request):
        token=self.token
        model=str(request.get('model') or self.model)
        if not token or not model: raise ProviderError('Replicate is not configured on the server.')
        provider_input=request.get('input',request)
        async with httpx.AsyncClient(timeout=30,follow_redirects=False) as c:
            r=await c.post(f'https://api.replicate.com/v1/models/{model}/predictions',headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},json={'input':provider_input})
        if r.status_code>=400:
            detail=r.text[:800].replace('\\n',' ')
            raise ProviderError(f'Replicate rejected the request ({r.status_code}): {detail}')
        d=r.json(); return GenerationResult(self.name,str(d.get('id','')),str(d.get('status','starting')),_output_url(d),d)
    async def status(self,job_id,status_url=None):
        if not self.token: raise ProviderError('Replicate is not configured.')
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as c:
            r=await c.get(status_url or f'https://api.replicate.com/v1/predictions/{job_id}',headers={'Authorization':f'Bearer {self.token}'})
        if r.status_code>=400: raise ProviderError(f'Replicate status failed ({r.status_code}).')
        d=r.json(); return GenerationResult(self.name,job_id,str(d.get('status','unknown')),_output_url(d),d)
class ConfiguredHTTPProvider:
    def __init__(self,name,url_env,token_env,status_url_env=None): self.name=name; self.url_env=url_env; self.token_env=token_env; self.status_url_env=status_url_env
    async def submit(self,request):
        url,token=os.getenv(self.url_env,''),os.getenv(self.token_env,'')
        if not url or not token: raise ProviderError(f'{self.name} is not configured on the server.')
        async with httpx.AsyncClient(timeout=30,follow_redirects=False) as c:
            r=await c.post(url,headers={'Authorization':f'Bearer {token}','Content-Type':'application/json'},json=request)
        if r.status_code>=400: raise ProviderError(f'{self.name} rejected the request ({r.status_code}).')
        d=r.json(); return GenerationResult(self.name,str(d.get('id') or d.get('job_id') or d.get('task_id') or ''),str(d.get('status','queued')),_output_url(d),d)
    async def status(self,job_id,status_url=None):
        url=''
        if status_url: url=status_url.replace('{id}',str(job_id))
        elif self.status_url_env: url=os.getenv(self.status_url_env,'').replace('{id}',str(job_id))
        if not url: raise ProviderError(f'{self.name} status polling is not configured. Set {self.status_url_env or self.name.upper()+"_STATUS_URL_TEMPLATE"}.')
        token=os.getenv(self.token_env,'')
        if not token: raise ProviderError(f'{self.name} is not configured on the server.')
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as c:
            r=await c.get(url,headers={'Authorization':f'Bearer {token}'})
        if r.status_code>=400: raise ProviderError(f'{self.name} status failed ({r.status_code}).')
        d=r.json(); return GenerationResult(self.name,job_id,str(d.get('status','unknown')),_output_url(d),d)
PROVIDERS={'replicate':ReplicateProvider(),'seedance':ConfiguredHTTPProvider('seedance','SEEDANCE_API_URL','SEEDANCE_API_TOKEN','SEEDANCE_STATUS_URL_TEMPLATE'),'runway':ConfiguredHTTPProvider('runway','RUNWAY_API_URL','RUNWAY_API_TOKEN','RUNWAY_STATUS_URL_TEMPLATE')}

# --- Background removal (image + video) -------------------------------------------------
# Kept independent of ReplicateProvider above deliberately: that class is hardcoded to a
# single REPLICATE_MODEL/one input shape for the main generation flow, so it can't also
# serve a fixed-model utility like background removal without either overloading its env
# var or adding a model parameter that would change its existing contract. Model versions
# are pinned (not just "owner/name") so an upstream model update can't silently change your
# output — verified against Replicate's own model pages, not assumed.
REMBG_IMAGE_VERSION = '34bd50c3cdcf667a839abdcdde7201d5b39bbebb54aa037da542ee6e670d9786'  # cjwbw/rembg
REMBG_VIDEO_MODEL = 'lucataco/rembg-video'  # no pinned version publicly listed at time of writing; pin one yourself once you've tested it

async def remove_background(image_url: str) -> GenerationResult:
    token = os.getenv('REPLICATE_API_TOKEN', '')
    if not token:
        raise ProviderError('Replicate is not configured on the server.')
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as c:
        r = await c.post(
            'https://api.replicate.com/v1/predictions',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Prefer': 'wait=25'},
            json={'version': REMBG_IMAGE_VERSION, 'input': {'image': image_url}},
        )
    if r.status_code >= 400:
        raise ProviderError(f'Background removal request rejected ({r.status_code}).')
    d = r.json()
    d = await _poll_replicate_prediction(d, token, timeout_seconds=90)
    out = d.get('output')
    out_url = out if isinstance(out, str) else None
    return GenerationResult('replicate-rembg', str(d.get('id', '')), str(d.get('status', 'starting')), out_url, d)

async def remove_background_video(video_url: str) -> GenerationResult:
    token = os.getenv('REPLICATE_API_TOKEN', '')
    if not token:
        raise ProviderError('Replicate is not configured on the server.')
    async with httpx.AsyncClient(timeout=60, follow_redirects=False) as c:
        r = await c.post(
            f'https://api.replicate.com/v1/models/{REMBG_VIDEO_MODEL}/predictions',
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
            json={'input': {'video': video_url}},
        )
    if r.status_code >= 400:
        raise ProviderError(f'Video background removal request rejected ({r.status_code}).')
    d = r.json()
    # Video takes meaningfully longer than the 25s "Prefer: wait" window above covers for
    # images, so this always needs an explicit poll loop, not just a longer wait header.
    d = await _poll_replicate_prediction(d, token, timeout_seconds=240)
    out = d.get('output')
    out_url = out if isinstance(out, str) else (out[0] if isinstance(out, list) and out and isinstance(out[0], str) else None)
    return GenerationResult('replicate-rembg-video', str(d.get('id', '')), str(d.get('status', 'starting')), out_url, d)

async def _poll_replicate_prediction(prediction: dict, token: str, timeout_seconds: int) -> dict:
    """Both background-removal calls above previously returned whatever status came back
    on the FIRST response — 'starting' or 'processing' for anything slower than the
    'Prefer: wait' window. There was no endpoint for a client to poll afterward, so a slow
    job (any video, or a cold-started image model) could never be observed reaching
    'succeeded'. This closes that gap by polling server-side until done or timeout, so the
    HTTP response from /api/remove-background is always a final result."""
    deadline = time.time() + timeout_seconds
    backoff = 1.5
    poll_url = (prediction.get('urls') or {}).get('get') or f"https://api.replicate.com/v1/predictions/{prediction.get('id')}"
    async with httpx.AsyncClient(timeout=30) as c:
        while prediction.get('status') not in ('succeeded', 'failed', 'canceled') and time.time() < deadline:
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.4, 6.0)
            resp = await c.get(poll_url, headers={'Authorization': f'Bearer {token}'})
            if resp.status_code < 400:
                prediction = resp.json()
    return prediction

# Capability metadata used by the Agent/provider router. An entry is considered
# production-usable only when its required server-side configuration exists.
PROVIDER_CAPABILITIES = {
    'replicate': {'capabilities': ['video','image','image-to-video','video-to-video'], 'configured_by': ['REPLICATE_API_TOKEN','REPLICATE_MODEL','REPLICATE_IMAGE_MODEL']},
    'seedance': {'capabilities': ['video'], 'configured_by': ['SEEDANCE_API_URL','SEEDANCE_API_TOKEN']},
    'runway': {'capabilities': ['video'], 'configured_by': ['RUNWAY_API_URL','RUNWAY_API_TOKEN']},
}
