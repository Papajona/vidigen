import json, os, re, time, uuid, secrets, logging, asyncio
from collections import deque
from pathlib import Path
from typing import Any
import httpx
from fastapi import FastAPI, HTTPException, Request, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, HTMLResponse
from pydantic import BaseModel, Field, ConfigDict
from starlette.middleware.base import BaseHTTPMiddleware
from gateway.providers import PROVIDERS, ProviderError
from gateway import persistence

COMFY_URL=os.getenv('COMFY_URL','http://127.0.0.1:8188').rstrip('/')
WORKFLOW=Path(os.getenv('WORKFLOW_FILE','workflow_api.json'))
MEMORY_FILE=Path(os.getenv('MEMORY_FILE','vidigen_memory.json'))
GATEWAY_TOKEN=os.getenv('VIDIGEN_GATEWAY_TOKEN','')
ALLOWED_ORIGINS=[x.strip() for x in os.getenv('VIDIGEN_ALLOWED_ORIGINS','http://localhost:5173,http://127.0.0.1:5173').split(',') if x.strip()]
MAX_PROMPT=4000
MAX_MEMORY=500
MAX_REQUESTS_PER_MINUTE=int(os.getenv('VIDIGEN_RATE_LIMIT','30'))
ADMIN_2FA_REQUIRED=os.getenv('VIDIGEN_ADMIN_2FA_REQUIRED','true').lower() in {'1','true','yes','on'}
MAX_RENDER_CLIPS=int(os.getenv('VIDIGEN_MAX_RENDER_CLIPS','50'))
MAX_RENDER_BYTES=int(os.getenv('VIDIGEN_MAX_RENDER_BYTES','500000000'))
RENDER_TIMEOUT_SECONDS=int(os.getenv('VIDIGEN_RENDER_TIMEOUT','900'))
RENDER_ALLOWED_HOSTS=[x.strip().lower() for x in os.getenv('VIDIGEN_RENDER_ALLOWED_HOSTS','').split(',') if x.strip()]

# --- Live Developer Logs foundation ---------------------------------------------------
# There was no logging infrastructure anywhere in this file before this — no `logging`
# calls at all — so "Live Developer Logs" had nothing to actually stream. This adds: an
# in-memory ring buffer (last 500 lines, for a client that just opened the dashboard and
# wants recent history) plus a broadcaster that pushes new lines to any connected SSE
# clients in real time via asyncio.Queue. Deliberately in-memory only, not a file or
# external log service — restarting the gateway clears history, which is an honest,
# stated limitation, not a hidden one.
_log_ring: deque = deque(maxlen=500)
_log_subscribers: set = set()

class BroadcastLogHandler(logging.Handler):
    def emit(self, record):
        try:
            line = self.format(record)
        except Exception:
            return
        _log_ring.append(line)
        for q in list(_log_subscribers):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                pass

_handler = BroadcastLogHandler()
_handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
logging.getLogger().addHandler(_handler)
logging.getLogger().setLevel(logging.INFO)
# Quiet third-party libraries' own INFO-level chatter (httpx logs one line per outbound
# call to Replicate/Groq/Supabase) so the dashboard's log stream shows this app's own
# events, not a flood of library internals drowning them out.
logging.getLogger('httpx').setLevel(logging.WARNING)
logging.getLogger('httpcore').setLevel(logging.WARNING)
log = logging.getLogger('vidigen.gateway')

# Detects a real, specific footgun rather than a generic "is this cloud?" guess: K_SERVICE
# is documented by Google as auto-injected into every Cloud Run container (confirmed against
# Cloud Run's own docs, not assumed) — so its presence is a reliable signal, not a heuristic.
# COMFY_URL pointing at a loopback address only ever works when a local ComfyUI process is
# reachable at that address, which is never true inside a Cloud Run container: nothing else
# is running in it. Without this check, that specific misconfiguration fails silently as
# ordinary-looking connection-refused errors on every generation request, with no signal
# pointing at the actual cause.
if os.getenv('K_SERVICE') and ('127.0.0.1' in COMFY_URL or 'localhost' in COMFY_URL):
    log.warning(
        f'COMFY_URL is set to a loopback address ({COMFY_URL}) but this process is running '
        'on Cloud Run (K_SERVICE is set) — there is no local ComfyUI instance reachable from '
        'inside this container, so any request routed to it will fail. Point COMFY_URL at a '
        'separately-hosted ComfyUI server if you need it, or leave generation to the '
        'cloud-provider routes (Replicate/Seedance/Runway) that do work here.'
    )


OLLAMA_URL=os.getenv('OLLAMA_URL','http://127.0.0.1:11434').rstrip('/')
OLLAMA_MODEL=os.getenv('OLLAMA_MODEL','llama3.2')
OUTPUT_CAP_TTL=int(os.getenv('VIDIGEN_OUTPUT_CAP_TTL','300'))
OUTPUT_CAPS={}
WHISPER_CACHE={}
CAPTION_PROVIDER=os.getenv('CAPTION_PROVIDER','local').lower()
WHISPER_MODEL=os.getenv('WHISPER_MODEL','small')
JOB_CACHE={}
LEARNED_OUTPUTS=set()
SUPABASE_JWT_ISSUER=os.getenv('SUPABASE_JWT_ISSUER','').rstrip('/')
SUPABASE_JWT_AUD=os.getenv('SUPABASE_JWT_AUD','authenticated')
MAX_AUDIO_BYTES=int(os.getenv('VIDIGEN_MAX_AUDIO_BYTES','50000000'))

SENTRY_DSN = os.getenv('SENTRY_DSN', '')
if SENTRY_DSN:
    import sentry_sdk
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        environment=os.getenv('VIDIGEN_ENVIRONMENT', 'production'),
        release=os.getenv('VIDIGEN_APP_VERSION', 'unknown'),
        traces_sample_rate=float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0.1')),
        send_default_pii=False,  # Never send request bodies/headers by default — this app handles auth tokens and user media.
    )

app=FastAPI(title='Vidigen Local Autonomous AI Gateway')

# Logs every 4xx/5xx and every unhandled exception automatically, rather than requiring a
# log call at each of the several dozen individual `raise HTTPException(...)` sites in this
# file — one central place, guaranteed coverage, and this is also the backend/API half of
# the "Error Monitoring" dashboard item (frontend errors are a separate concern — see
# ADMIN_DASHBOARD.md for what's still not covered).
#
# IMPORTANT: these handlers explicitly call sentry_sdk.capture_exception() rather than
# relying on Sentry's automatic ASGI-level capture. Verified against Sentry's own guidance:
# a custom FastAPI exception handler intercepts an exception before it would ever reach
# Sentry's automatic instrumentation, so without an explicit capture call here, Sentry would
# silently receive nothing at all despite being "configured" — a real, easy-to-miss gap for
# exactly this kind of setup (custom handler + Sentry added afterward).
from starlette.exceptions import HTTPException as _StarletteHTTPException
from fastapi.exception_handlers import http_exception_handler as _default_http_exception_handler

@app.exception_handler(_StarletteHTTPException)
async def _logged_http_exception_handler(request: Request, exc: _StarletteHTTPException):
    if exc.status_code >= 400:
        level = log.warning if exc.status_code < 500 else log.error
        level(f'{request.method} {request.url.path} -> {exc.status_code}: {exc.detail}')
        if exc.status_code >= 500 and SENTRY_DSN:
            import sentry_sdk
            sentry_sdk.capture_exception(exc)
    return await _default_http_exception_handler(request, exc)

@app.exception_handler(Exception)
async def _logged_unhandled_exception_handler(request: Request, exc: Exception):
    log.error(f'{request.method} {request.url.path} -> UNHANDLED {type(exc).__name__}: {exc}')
    if SENTRY_DSN:
        import sentry_sdk
        sentry_sdk.capture_exception(exc)
    return JSONResponse({'detail': 'Internal server error.'}, status_code=500)

app.add_middleware(CORSMiddleware,allow_origins=ALLOWED_ORIGINS,allow_credentials=False,allow_methods=['GET','POST','PATCH','DELETE','OPTIONS'],allow_headers=['Authorization','Content-Type','X-Admin-2FA-Session'])

class SecurityHeadersMiddleware(BaseHTTPMiddleware):
 async def dispatch(self,request,call_next):
  response=await call_next(request)
  response.headers['X-Content-Type-Options']='nosniff'; response.headers['X-Frame-Options']='DENY'; response.headers['Referrer-Policy']='no-referrer'; response.headers['Cache-Control']='no-store'
  return response
app.add_middleware(SecurityHeadersMiddleware)

class RateLimitMiddleware(BaseHTTPMiddleware):
 def __init__(self,app): super().__init__(app); self.hits={}
 async def dispatch(self,request,call_next):
  if request.url.path.startswith('/api/') and not request.url.path.startswith('/api/status/'):
   key=request.client.host if request.client else 'unknown'; now=time.time(); bucket=[t for t in self.hits.get(key,[]) if now-t<60]
   if len(bucket)>=MAX_REQUESTS_PER_MINUTE:return JSONResponse({'detail':'Rate limit exceeded'},status_code=429)
   bucket.append(now); self.hits[key]=bucket
  return await call_next(request)
app.add_middleware(RateLimitMiddleware)

class MaintenanceModeMiddleware(BaseHTTPMiddleware):
    """Piggybacks on the Feature Flags system already built rather than a separate
    mechanism — a 'maintenance_mode' flag, toggled the same way any other flag is.
    Cached with a short TTL so this doesn't add a Supabase round-trip to every single
    request; a flag flip takes up to CACHE_SECONDS to actually take effect everywhere.
    Admin routes, health, and docs stay reachable during maintenance — otherwise an admin
    who just turned maintenance mode on would immediately lock themselves out of turning
    it back off."""
    CACHE_SECONDS = 10
    _cached_value = False
    _cached_at = 0.0
    EXEMPT_PREFIXES = ('/api/admin', '/health', '/docs', '/openapi.json', '/redoc')

    async def dispatch(self, request, call_next):
        if not request.url.path.startswith('/api/') or any(request.url.path.startswith(p) for p in self.EXEMPT_PREFIXES):
            return await call_next(request)
        now = time.time()
        if now - MaintenanceModeMiddleware._cached_at > self.CACHE_SECONDS:
            MaintenanceModeMiddleware._cached_at = now
            try:
                if persistence.enabled():
                    flags = await persistence.list_flags()
                    MaintenanceModeMiddleware._cached_value = any(f.get('key') == 'maintenance_mode' and f.get('enabled') for f in flags)
            except Exception:
                pass  # Keep serving on a flag-check failure — never let this middleware itself take the app down.
        if MaintenanceModeMiddleware._cached_value:
            return JSONResponse({'detail': 'Vidigen is temporarily down for maintenance. Please try again shortly.'}, status_code=503)
        return await call_next(request)
app.add_middleware(MaintenanceModeMiddleware)

class Generate(BaseModel):
 model_config=ConfigDict(extra='forbid')
 prompt:str=Field(min_length=1,max_length=MAX_PROMPT)
 mode:str=Field(default='Text → Video',max_length=64)
 ratio:str=Field(default='16:9',pattern=r'^(16:9|9:16|1:1|4:5|21:9)$')
 duration:str=Field(default='5s',pattern=r'^[1-9][0-9]?s$')
 scene:dict[str,Any]=Field(default_factory=dict)
 sourceUrl:str|None=Field(default=None,max_length=2048)
 model:str=Field(default='auto',max_length=64)
 # Optional client-supplied key so a retried request (double-click, flaky network, client
 # retry logic) doesn't create a second billed job. Scoped per-user — same key from two
 # different users is not a collision. Not required: omitting it just means no dedup for
 # that request, same as before this field existed.
 idempotencyKey:str|None=Field(default=None,min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_-]{1,128}$')

class Memory(BaseModel):
 model_config=ConfigDict(extra='forbid')
 prompt:str=Field(min_length=1,max_length=MAX_PROMPT)
 mode:str=Field(default='Text → Video',max_length=64)
 rating:int=Field(default=0,ge=0,le=5)
 tags:list[str]=Field(default_factory=list,max_length=20)
 success:bool=True

class Analyze(BaseModel):
 model_config=ConfigDict(extra='forbid')
 prompt:str=Field(min_length=1,max_length=MAX_PROMPT)

class GeminiRequest(BaseModel):
 model_config=ConfigDict(extra='forbid')
 prompt:str=Field(min_length=1,max_length=MAX_PROMPT)
 mode:str|None=Field(default=None,max_length=64)
 model:str|None=Field(default=None,max_length=128)

class EditPlan(BaseModel):
 model_config=ConfigDict(extra='forbid')
 command:str=Field(min_length=1,max_length=1000)
 clips:list[dict[str,Any]]=Field(default_factory=list,max_length=200)
 ratio:str=Field(default='16:9',pattern=r'^(16:9|9:16|1:1|4:5|21:9)$')

class ExportPlan(BaseModel):
 model_config=ConfigDict(extra='forbid')
 ratio:str=Field(default='16:9',pattern=r'^(16:9|9:16|1:1|4:5|21:9)$')
 clips:list[dict[str,Any]]=Field(default_factory=list,max_length=200)
 captionCount:int=Field(default=0,ge=0,le=10000)
 hasAudio:bool=False


def _verify_bearer_token(token: str):
 """Core token verification, taking a raw token string rather than reading it from the
 request header. Extracted so the SSE log-stream endpoint (see admin_logs_stream) can
 verify a token passed via query string — EventSource cannot send custom headers, so
 that's the only way browser-native SSE can authenticate at all. Query-string tokens are
 otherwise avoided in this codebase (they can leak into server access logs and browser
 history) — this is the one deliberate, narrow exception, scoped to a single route, not a
 general alternative to header-based auth."""
 if not token: return None
 if GATEWAY_TOKEN and secrets.compare_digest(token, GATEWAY_TOKEN): return {'sub':'local-gateway','role':'gateway'}
 if not persistence.enabled() or not SUPABASE_JWT_ISSUER: return None
 try:
  import jwt
  # SECURITY: 'HS256' must never be in this list. get_signing_key_from_jwt() below returns
  # a PUBLIC key fetched from the JWKS endpoint — that's the entire point of JWKS, those
  # keys are meant to be public. HS256 is symmetric: the same key signs and verifies. If it
  # were allowed here, anyone could fetch the (public, non-secret) JWKS key, forge a token
  # with header {"alg":"HS256"}, sign it using that public key as an HMAC secret, and this
  # code would accept it as valid — a full authentication bypass letting an attacker mint a
  # token for any `sub` (any user id) they choose. This is a well-documented, real-world
  # vulnerability class ("RS256-to-HS256 key confusion"), not a hypothetical.
  # A JWKS-sourced key is only ever valid for asymmetric algorithms.
  key=_jwks_client().get_signing_key_from_jwt(token).key
  claims=jwt.decode(token,key,algorithms=['ES256','RS256'],audience=SUPABASE_JWT_AUD,issuer=SUPABASE_JWT_ISSUER)
  return claims if claims.get('sub') else None
 except Exception:
  return None

def _jwt_user(request:Request):
 # Supabase JWT verification is optional and enabled only when a project URL + PyJWT are configured.
 header=request.headers.get('authorization','')
 if not header.startswith('Bearer '): return None
 return _verify_bearer_token(header[7:].strip())

_JWKS_CLIENT_CACHE={}
def _jwks_client():
 # Previously this created a fresh PyJWKClient (and made a fresh HTTP request to the JWKS
 # endpoint) on EVERY authenticated request, with an earlier synchronous httpx.get() call
 # right before it that fetched the same JWKS document and then never used the result at
 # all (dead code, and a wasted network round-trip on every single API call). Caching one
 # client per issuer means the JWKS document is fetched once and reused, with PyJWT's own
 # built-in cache handling refresh when a token references an unrecognized `kid`.
 import jwt
 if SUPABASE_JWT_ISSUER not in _JWKS_CLIENT_CACHE:
  jwks_url=f'{SUPABASE_JWT_ISSUER}/auth/v1/.well-known/jwks.json'
  _JWKS_CLIENT_CACHE[SUPABASE_JWT_ISSUER]=jwt.PyJWKClient(jwks_url,cache_keys=True)
 return _JWKS_CLIENT_CACHE[SUPABASE_JWT_ISSUER]

def auth(request:Request):
 user=_jwt_user(request)
 if user: request.state.user=user; return user
 if not GATEWAY_TOKEN:
  host=request.client.host if request.client else ''
  if host not in {'127.0.0.1','::1','localhost'}: raise HTTPException(401,'Set VIDIGEN_GATEWAY_TOKEN or configure Supabase authentication for non-loopback access.')
  request.state.user=None; return None
 raise HTTPException(401,'Invalid gateway token or Supabase access token.')

class RenderClip(BaseModel):
    model_config=ConfigDict(extra='forbid')
    uri: str = Field(min_length=8, max_length=4000)
    trimStartMs: int = Field(default=0, ge=0, le=86_400_000)
    trimEndMs: int | None = Field(default=None, ge=1, le=86_400_000)

class RenderRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    ratio: str = Field(default='16:9', pattern=r'^(16:9|9:16|1:1|4:5|21:9)$')
    clips: list[RenderClip] = Field(min_length=1, max_length=MAX_RENDER_CLIPS)
    backgroundAudioUri: str | None = Field(default=None, max_length=4000)


def _ratio_filter(ratio: str) -> str:
    sizes = {'16:9':(1280,720), '9:16':(720,1280), '1:1':(1080,1080), '4:5':(1080,1350), '21:9':(1280,548)}
    w,h = sizes[ratio]
    # Scale-to-fit with letterbox; keeps the entire source frame and normalizes output dimensions.
    return f"scale=w={w}:h={h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p"

async def _download_render_source(uri: str, target: Path) -> None:
    import urllib.parse, ipaddress, socket
    parsed = urllib.parse.urlparse(uri)
    if parsed.scheme not in ('http','https') or not parsed.hostname:
        raise HTTPException(400, 'Render sources must be http(s) URLs. Upload local media to R2 before rendering in the browser.')
    host = parsed.hostname.lower().rstrip('.')
    allowed = set(RENDER_ALLOWED_HOSTS)
    if not allowed:
        public_base=os.getenv('R2_PUBLIC_BASE_URL','').rstrip('/')
        try:
            pb=urllib.parse.urlparse(public_base)
            if pb.hostname: allowed.add(pb.hostname.lower().rstrip('.'))
        except Exception: pass
    if not allowed:
        raise HTTPException(501, 'Set VIDIGEN_RENDER_ALLOWED_HOSTS (or R2_PUBLIC_BASE_URL) before enabling server rendering.')
    if host not in allowed and not any(host.endswith('.'+suffix.lstrip('.')) for suffix in allowed if suffix.startswith('.')):
        raise HTTPException(403, 'Render source host is not allowlisted.')
    try:
        for family,_,_,_,sockaddr in socket.getaddrinfo(host,443 if parsed.scheme=='https' else 80,type=socket.SOCK_STREAM):
            ip=ipaddress.ip_address(sockaddr[0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
                raise HTTPException(403, 'Render source resolves to a private or reserved network address.')
    except socket.gaierror:
        raise HTTPException(502, 'Could not resolve render source host.')
    async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
        total = 0
        async with client.stream('GET', uri) as resp:
            if resp.status_code >= 400:
                raise HTTPException(502, f'Could not fetch render source ({resp.status_code}).')
            with target.open('wb') as fh:
                async for chunk in resp.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_RENDER_BYTES:
                        raise HTTPException(413, 'Render source exceeds the configured size limit.')
                    fh.write(chunk)


def _run_ffmpeg_render(inputs: list[tuple[str,int,int|None]], ratio: str, audio_file: str|None, output: str) -> dict:
    import subprocess, tempfile
    tmp_parts=[]
    try:
        for idx,(src,start_ms,end_ms) in enumerate(inputs):
            part = str(Path(output).with_name(f'part-{idx}.mp4'))
            probe_audio=subprocess.run(['ffprobe','-v','error','-select_streams','a:0','-show_entries','stream=index','-of','csv=p=0',src],capture_output=True,text=True,timeout=30)
            has_audio=bool(probe_audio.stdout.strip())
            cmd=['ffmpeg','-y','-ss',str(start_ms/1000.0),'-i',src]
            if has_audio:
                cmd += ['-map','0:v:0','-map','0:a:0','-vf',_ratio_filter(ratio),'-c:v','libx264','-preset','veryfast','-crf','20','-c:a','aac','-b:a','128k','-shortest','-movflags','+faststart']
            else:
                cmd += ['-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000','-map','0:v:0','-map','1:a:0','-vf',_ratio_filter(ratio),'-c:v','libx264','-preset','veryfast','-crf','20','-c:a','aac','-b:a','128k','-shortest','-movflags','+faststart']
            if end_ms is not None:
                dur=max(0.1,(end_ms-start_ms)/1000.0)
                cmd += ['-t',str(dur)]
            cmd += [part]
            r=subprocess.run(cmd,capture_output=True,text=True,timeout=RENDER_TIMEOUT_SECONDS)
            if r.returncode != 0 or not Path(part).exists():
                raise RuntimeError(f'ffmpeg clip {idx+1} failed: {r.stderr[-1200:]}')
            tmp_parts.append(part)
        concat_file = str(Path(output).with_name('concat.txt'))
        Path(concat_file).write_text('\n'.join("file '"+x.replace("'","'\\''")+"'" for x in tmp_parts), encoding='utf-8')
        joined = str(Path(output).with_name('joined.mp4'))
        r=subprocess.run(['ffmpeg','-y','-f','concat','-safe','0','-i',concat_file,'-c','copy',joined],capture_output=True,text=True,timeout=RENDER_TIMEOUT_SECONDS)
        if r.returncode != 0 or not Path(joined).exists():
            raise RuntimeError(f'ffmpeg concat failed: {r.stderr[-1200:]}')
        if audio_file:
            cmd=['ffmpeg','-y','-i',joined,'-stream_loop','-1','-i',audio_file,'-map','0:v:0','-map','1:a:0','-c:v','copy','-c:a','aac','-b:a','160k','-shortest','-movflags','+faststart',output]
        else:
            cmd=['ffmpeg','-y','-i',joined,'-c','copy','-movflags','+faststart',output]
        r=subprocess.run(cmd,capture_output=True,text=True,timeout=RENDER_TIMEOUT_SECONDS)
        if r.returncode != 0 or not Path(output).exists():
            raise RuntimeError(f'ffmpeg finalization failed: {r.stderr[-1200:]}')
        probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration,size','-of','json',output],capture_output=True,text=True,timeout=60)
        meta=json.loads(probe.stdout or '{}').get('format',{})
        size=int(meta.get('size') or Path(output).stat().st_size)
        if size > MAX_RENDER_BYTES:
            raise RuntimeError('Rendered master exceeds the configured output-size limit.')
        return {'duration': float(meta.get('duration') or 0), 'bytes': size}
    finally:
        for f in tmp_parts + [str(Path(output).with_name('concat.txt')), str(Path(output).with_name('joined.mp4'))]:
            try: Path(f).unlink(missing_ok=True)
            except Exception: pass

async def _upload_r2_file(local_path: str, uid: str, content_type='video/mp4') -> dict:
    endpoint,access_key,secret_key,bucket = (os.getenv(k,'') for k in ('R2_ENDPOINT','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','R2_BUCKET'))
    cdn_base=os.getenv('R2_PUBLIC_BASE_URL','').rstrip('/')
    if not all((endpoint,access_key,secret_key,bucket,cdn_base)):
        raise HTTPException(501,'R2 storage with R2_PUBLIC_BASE_URL is required for durable gateway renders.')
    try:
        import boto3
        from botocore.config import Config as BotoConfig
        s3=boto3.client('s3',endpoint_url=endpoint,aws_access_key_id=access_key,aws_secret_access_key=secret_key,config=BotoConfig(signature_version='s3v4'),region_name='auto')
    except ImportError:
        raise HTTPException(503,'boto3 is not installed on the gateway.')
    key=f'users/{uid}/exports/{uuid.uuid4().hex}.mp4'
    await asyncio.to_thread(s3.upload_file, local_path, bucket, key, ExtraArgs={'ContentType':content_type})
    return {'storage_key':key,'output_url':f'{cdn_base}/{key}'}

@app.post('/api/render')
async def render_project(req: RenderRequest, request: Request, user=Depends(auth)):
    """Real server-side MP4 render for browser/web deployments. Sources must be durable http(s) URLs (normally R2/CDN)."""
    import shutil, tempfile
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise HTTPException(503,'ffmpeg and ffprobe are required on the gateway host for web rendering.')
    uid=(user or {}).get('sub') if isinstance(user,dict) else None
    if not uid:
        raise HTTPException(401,'Signed-in user required for durable rendering.')
    with tempfile.TemporaryDirectory(prefix='vidigen-render-') as tmp:
        source_pairs=[]
        for i,clip in enumerate(req.clips):
            src=Path(tmp)/f'source-{i}.bin'
            await _download_render_source(clip.uri,src)
            source_pairs.append((str(src),clip.trimStartMs,clip.trimEndMs))
        audio_file=None
        if req.backgroundAudioUri:
            ap=Path(tmp)/'audio.bin'; await _download_render_source(req.backgroundAudioUri,ap); audio_file=str(ap)
        out=Path(tmp)/'master.mp4'
        try:
            meta=await asyncio.to_thread(_run_ffmpeg_render,source_pairs,req.ratio,audio_file,str(out))
        except asyncio.TimeoutError:
            raise HTTPException(504,'Render timed out.')
        except Exception as exc:
            raise HTTPException(500,f'Render failed: {exc}')
        uploaded=await _upload_r2_file(str(out),uid)
        if persistence.enabled():
            try:
                await persistence.create_asset(uid,None,'export',uploaded['storage_key'],'video/mp4',meta['bytes'],{'duration':meta['duration'],'ratio':req.ratio,'source_count':len(req.clips)})
            except Exception:
                log.exception('Render completed but asset metadata could not be persisted')
        return {'ok':True,'status':'completed',**uploaded,**meta,'ratio':req.ratio}


async def require_admin(request: Request, user=Depends(auth)):
    """Gate for every /api/admin/* route. The local GATEWAY_TOKEN path (used for loopback
    dev access) is intentionally never treated as admin — that shortcut exists for
    convenience during local development, not for granting control-panel access. Admin
    status is checked via Supabase's admin_users table, which has no client-facing RLS
    policy at all (see supabase_schema.sql) — so this check can only ever be performed here,
    server-side, with the service-role key.

    2FA note: if an admin has enrolled 2FA (admin_2fa), a valid, unexpired session from
    /api/admin/2fa/verify is ALSO required (via X-Admin-2FA-Session header) — the JWT alone
    is no longer sufficient once 2FA is turned on for that account. If an admin has NOT
    enrolled 2FA, they pass with just the JWT, same as before — 2FA here is opt-in per
    admin, not yet mandatory account-wide. That's a real, stated gap, not a hidden one:
    an admin who never enrolls 2FA is not actually protected by it."""
    if not isinstance(user, dict) or user.get('role') == 'gateway':
        raise HTTPException(403, 'Admin access requires a Supabase-authenticated admin account.')
    uid = user.get('sub')
    if not uid or not await persistence.is_admin(uid):
        raise HTTPException(403, 'This account is not registered as an admin.')
    enrolled = await persistence.has_2fa_enrolled(uid)
    if ADMIN_2FA_REQUIRED and not enrolled:
        raise HTTPException(428, 'Admin 2FA enrollment is required before using the control panel.')
    if enrolled:
        session_token = request.headers.get('x-admin-2fa-session', '')
        if not await persistence.check_2fa_session(session_token, uid):
            raise HTTPException(401, '2FA session required or expired. Verify a TOTP code at /api/admin/2fa/verify first.')
    return user

class TwoFactorVerifyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    code: str = Field(min_length=6, max_length=6)

@app.post('/api/admin/2fa/enroll')
async def admin_2fa_enroll(request: Request, user=Depends(auth)):
    """Enrollment intentionally does NOT go through require_admin (which would demand an
    already-active 2FA session — impossible before enrollment exists). It only requires
    being a verified admin via JWT, which is the correct bar for turning 2FA on in the
    first place."""
    if not isinstance(user, dict) or user.get('role') == 'gateway':
        raise HTTPException(403, 'Admin access requires a Supabase-authenticated admin account.')
    uid = user.get('sub')
    if not uid or not await persistence.is_admin(uid):
        raise HTTPException(403, 'This account is not registered as an admin.')
    if await persistence.has_2fa_enrolled(uid):
        raise HTTPException(409, '2FA is already enrolled for this account. Contact another admin to reset it if you lost access.')
    from gateway.two_factor import generate_secret, provisioning_uri
    secret = generate_secret()
    await persistence.enroll_2fa(uid, secret)
    await _audit(user, 'enroll_2fa', {})
    log.warning(f'2FA enrolled for admin {uid}')
    return {'secret': secret, 'provisioning_uri': provisioning_uri(secret, user.get('email', uid))}

@app.post('/api/admin/2fa/verify')
async def admin_2fa_verify(req: TwoFactorVerifyRequest, request: Request, user=Depends(auth)):
    if not isinstance(user, dict) or user.get('role') == 'gateway':
        raise HTTPException(403, 'Admin access requires a Supabase-authenticated admin account.')
    uid = user.get('sub')
    if not uid or not await persistence.is_admin(uid):
        raise HTTPException(403, 'This account is not registered as an admin.')
    secret = await persistence.get_2fa_secret(uid)
    if not secret:
        raise HTTPException(400, '2FA is not enrolled for this account yet.')
    # Account-level lockout, additive to the gateway's IP-based RateLimitMiddleware: a
    # 6-digit TOTP code (1,000,000 combinations) is guessable within a rate limiter's budget
    # if an attacker spreads attempts across enough source IPs. Locking the account itself
    # closes that gap regardless of how many IPs are used.
    locked, locked_until = await persistence.is_2fa_locked(uid)
    if locked:
        log.warning(f'2FA verify attempt for locked-out admin {uid} (locked_until={locked_until})')
        raise HTTPException(429, f'Too many failed codes. This account is locked until {locked_until}.')
    from gateway.two_factor import verify_code, generate_session_token, session_expiry
    if not verify_code(secret, req.code):
        await persistence.record_2fa_failure(uid)
        log.warning(f'Failed 2FA attempt for admin {uid}')
        raise HTTPException(401, 'Invalid or expired code.')
    await persistence.reset_2fa_failures(uid)
    session_token = generate_session_token()
    expires_at = session_expiry(hours=4)
    await persistence.create_2fa_session(uid, session_token, expires_at.isoformat())
    log.info(f'2FA verified for admin {uid}, session issued for 4h')
    return {'session_token': session_token, 'expires_at': expires_at.isoformat()}

@app.get('/admin', response_class=HTMLResponse)
async def admin_dashboard_page():
    """Serves the developer dashboard HTML. Deliberately NOT gated by require_admin at this
    route level — the page itself has no privileges; every action it takes calls a real
    /api/admin/* endpoint that IS gated. Serving the empty shell to an unauthenticated
    visitor reveals nothing (not even whether Supabase/R2/Replicate are configured — that
    only loads after a valid admin token is supplied and accepted by the API itself)."""
    html_path = Path(__file__).parent / 'admin_dashboard.html'
    return HTMLResponse(html_path.read_text())

@app.get('/api/admin/flags')
async def admin_list_flags(request: Request, _=Depends(require_admin)):
    return {'flags': await persistence.list_flags()}

class SetFlagRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=100, pattern=r'^[a-z][a-z0-9_]*$')
    enabled: bool
    description: str | None = Field(default=None, max_length=500)

@app.post('/api/admin/flags')
async def admin_set_flag(req: SetFlagRequest, request: Request, user=Depends(require_admin)):
    result = await persistence.set_flag(req.key, req.enabled, req.description)
    await _audit(user, 'set_feature_flag', {'key': req.key, 'enabled': req.enabled})
    log.info(f'Feature flag changed: {req.key} = {req.enabled}')
    return {'flag': result}

@app.get('/api/flags')
async def public_flags(request: Request, user=Depends(auth)):
    """Read-only, any signed-in user — this is what the app itself checks to know which
    features are live. Deliberately separate from /api/admin/flags, which returns full
    metadata and requires admin status."""
    if not user:
        raise HTTPException(401, 'Sign in required.')
    flags = await persistence.list_flags()
    return {'flags': {f['key']: f['enabled'] for f in flags}}

@app.get('/api/admin/health')
async def admin_health(request: Request, _=Depends(require_admin)):
    """Same provider-configured checks as the public /api/providers, plus what that route
    intentionally omits: whether Supabase persistence and admin auth are actually working
    right now, not just whether their env vars are set."""
    providers_status = {
        'replicate': bool(os.getenv('REPLICATE_API_TOKEN')) and bool(os.getenv('REPLICATE_MODEL')),
        'seedance': bool(os.getenv('SEEDANCE_API_URL')) and bool(os.getenv('SEEDANCE_API_TOKEN')),
        'runway': bool(os.getenv('RUNWAY_API_URL')) and bool(os.getenv('RUNWAY_API_TOKEN')),
    }
    supabase_reachable = False
    if persistence.enabled():
        try:
            await persistence.sb_request('GET', 'feature_flags', params={'select': 'key', 'limit': 1})
            supabase_reachable = True
        except persistence.PersistenceError:
            supabase_reachable = False
    return {
        'providers': providers_status,
        'replicate_token_set': bool(os.getenv('REPLICATE_API_TOKEN')),
        'groq_configured': bool(os.getenv('GROQ_API_KEY')),
        'r2_configured': all(os.getenv(k) for k in ('R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET')),
        'supabase_configured': persistence.enabled(),
        'supabase_reachable': supabase_reachable,
        'ffmpeg_available': __import__('shutil').which('ffmpeg') is not None,
    }

@app.get('/api/admin/version')
async def admin_version(request: Request, _=Depends(require_admin)):
    return {
        'app_version': os.getenv('VIDIGEN_APP_VERSION', 'unknown — set VIDIGEN_APP_VERSION on the gateway'),
        'gateway_routes': len(app.routes),
        'python_gateway_file_mtime': os.path.getmtime(__file__),
    }

# --- Automated Testing runner ---------------------------------------------------------
# A fixed, hardcoded whitelist of specific commands — deliberately NOT a general
# "run this shell command" endpoint. The security requirement isn't "the admin is trusted";
# it's that an admin's browser session (or a stolen admin token) shouldn't become a way to
# execute arbitrary code on the gateway host. Each entry is a literal argv list, never
# built from a client-supplied string, so there's no injection surface here at all.
TEST_SUITES = {
    'js_tests': ['node', '--test', 'tests/learning.test.mjs'],
    'python_reframe_tests': ['python3', '-m', 'unittest', 'tests.test_reframe'],
    'python_edit_ops_tests': ['python3', '-m', 'unittest', 'tests.test_edit_ops'],
    'python_2fa_tests': ['python3', '-m', 'unittest', 'tests.test_two_factor'],
}

@app.get('/api/admin/tests')
async def admin_list_tests(request: Request, _=Depends(require_admin)):
    return {'available': list(TEST_SUITES.keys())}

class RunTestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    suite: str

@app.post('/api/admin/tests/run')
async def admin_run_test(req: RunTestRequest, request: Request, user=Depends(require_admin)):
    if req.suite not in TEST_SUITES:
        raise HTTPException(400, f'Unknown suite. Available: {list(TEST_SUITES.keys())}')
    import subprocess
    cmd = TEST_SUITES[req.suite]
    log.info(f'Admin {user.get("sub")} running test suite: {req.suite}')
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=str(Path(__file__).parent.parent))
    except subprocess.TimeoutExpired:
        await _audit(user, 'run_test_suite', {'suite': req.suite, 'result': 'timeout'})
        raise HTTPException(504, f'Test suite {req.suite} timed out after 120s.')
    await _audit(user, 'run_test_suite', {'suite': req.suite, 'exit_code': result.returncode})
    log.info(f'Test suite {req.suite} finished with exit code {result.returncode}')
    return {'suite': req.suite, 'exit_code': result.returncode, 'passed': result.returncode == 0,
            'stdout': result.stdout[-8000:], 'stderr': result.stderr[-4000:]}

@app.get('/api/admin/logs/recent')
async def admin_logs_recent(request: Request, _=Depends(require_admin)):
    return {'lines': list(_log_ring)}

@app.get('/api/admin/logs/stream')
async def admin_logs_stream(request: Request):
    """Server-Sent Events — a real push stream, not polling. Plain SSE over the existing
    HTTP stack rather than adding a websocket dependency this project doesn't otherwise need.

    Auth here is deliberately NOT the usual Depends(require_admin) — EventSource (the
    browser API that consumes SSE) cannot send an Authorization header at all, so the token
    has to come from a query parameter for this one connection. That's verified through the
    same _verify_bearer_token() used everywhere else, and the same admin_users check —
    just sourced from a query param instead of a header for this single narrow case."""
    token = request.query_params.get('token', '')
    user = _verify_bearer_token(token)
    if not user or user.get('role') == 'gateway' or not await persistence.is_admin(user.get('sub', '')):
        raise HTTPException(403, 'Admin access requires a Supabase-authenticated admin account.')

    from fastapi.responses import StreamingResponse
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _log_subscribers.add(queue)

    async def event_source():
        try:
            for line in list(_log_ring)[-20:]:
                yield f'data: {line}\n\n'
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f'data: {line}\n\n'
                except asyncio.TimeoutError:
                    yield ': keepalive\n\n'
        finally:
            _log_subscribers.discard(queue)

    return StreamingResponse(event_source(), media_type='text/event-stream')

async def _audit(user: dict, action: str, detail: dict | None = None):
    uid = user.get('sub') if isinstance(user, dict) else None
    if uid:
        await persistence.log_admin_action(uid, action, detail)

@app.get('/api/admin/audit-log')
async def admin_get_audit_log(request: Request, _=Depends(require_admin)):
    return {'entries': await persistence.list_audit_log()}

@app.get('/api/admin/users')
async def admin_list_users(request: Request, page: int = 1, _=Depends(require_admin)):
    return await persistence.list_supabase_users(page=page)

@app.get('/api/admin/users/{uid}/profile')
async def admin_get_user_profile(uid: str, request: Request, _=Depends(require_admin)):
    profile = await persistence.get_profile_for_uid(uid)
    if not profile:
        raise HTTPException(404, 'No profile found for this uid.')
    return profile

@app.delete('/api/admin/users/{uid}')
async def admin_delete_user(uid: str, request: Request, user=Depends(require_admin)):
    if uid == user.get('sub'):
        raise HTTPException(400, "Can't delete your own admin account from this endpoint.")
    storage_result = await _cleanup_r2_user(uid)
    await persistence.delete_supabase_user(uid)
    await _audit(user, 'delete_user', {'uid': uid, 'r2_cleanup': storage_result})
    log.warning(f'Admin {user.get("sub")} deleted user {uid}; R2 cleanup={storage_result}')
    return {'deleted': uid, 'r2_cleanup': storage_result}

@app.get('/api/admin/release-readiness')
async def admin_release_readiness(request: Request, _=Depends(require_admin)):
    """Deterministic production gate: reports only checks the gateway can actually verify."""
    import shutil
    providers = {
        'replicate': bool(os.getenv('REPLICATE_API_TOKEN') and os.getenv('REPLICATE_MODEL')),
        'seedance': bool(os.getenv('SEEDANCE_API_URL') and os.getenv('SEEDANCE_API_TOKEN')),
        'runway': bool(os.getenv('RUNWAY_API_URL') and os.getenv('RUNWAY_API_TOKEN')),
        'local_comfyui': WORKFLOW.exists(),
    }
    supabase_ok = False
    if persistence.enabled():
        try:
            await persistence.sb_request('GET', 'admin_users', params={'select':'uid', 'limit':'1'})
            supabase_ok = True
        except Exception:
            supabase_ok = False
    r2_ok = all(os.getenv(k) for k in ('R2_ENDPOINT','R2_ACCESS_KEY_ID','R2_SECRET_ACCESS_KEY','R2_BUCKET','R2_PUBLIC_BASE_URL'))
    ffmpeg_ok = bool(shutil.which('ffmpeg')) and bool(shutil.which('ffprobe'))
    pillow_ok = False
    try:
        from PIL import Image  # noqa: F401
        pillow_ok = True
    except Exception:
        pass
    daily_entitlements_ok = False
    if persistence.enabled():
        try:
            await persistence.sb_request('GET','daily_feature_usage',params={'select':'user_id','limit':'1'})
            daily_entitlements_ok = True
        except Exception:
            daily_entitlements_ok = False
    whisper_ok = False
    try:
        import faster_whisper  # noqa: F401
        whisper_ok = True
    except Exception:
        pass
    from gateway import billing as _billing
    from gateway import moderation as _moderation
    # Output moderation now fails CLOSED by default (VIDIGEN_MODERATION_ENFORCE=true) — a
    # missing REPLICATE_API_TOKEN means every generation gets blocked at delivery, not
    # silently allowed through. That's the correct default, but it also means an
    # unconfigured token is now a real outage, not a soft gap — so it belongs in the
    # readiness gate rather than something an operator only discovers via a wave of
    # blocked-output support tickets.
    output_moderation_ready = (not _moderation.MODERATION_ENFORCE) or bool(os.getenv('REPLICATE_API_TOKEN'))
    checks = {
        'supabase_configured': persistence.enabled(),
        'supabase_reachable': supabase_ok,
        'r2_configured': r2_ok,
        'render_source_allowlist': bool(RENDER_ALLOWED_HOSTS or os.getenv('R2_PUBLIC_BASE_URL')),
        'ffmpeg_and_ffprobe': ffmpeg_ok,
        'captions_engine_installed': whisper_ok,
        'photo_enhancement_engine_installed': pillow_ok,
        'free_daily_entitlements_ready': daily_entitlements_ok,
        'real_generation_route_available': any(providers.values()),
        'admin_2fa_required': ADMIN_2FA_REQUIRED,
        'output_moderation_ready': output_moderation_ready,
        'gemini_server_configured': bool(os.getenv('GEMINI_API_KEY')),
    }
    moderation_status = {
        'enforce': _moderation.MODERATION_ENFORCE,
        'prompt_moderation_engine': 'groq' if os.getenv('GROQ_API_KEY') else 'keyword-fallback (narrow coverage — see gateway/moderation.py)',
        'output_moderation_configured': bool(os.getenv('REPLICATE_API_TOKEN')),
    }
    # Billing status is informational, not a readiness gate: a dev/test deployment that
    # hasn't configured live Paystack keys yet is still legitimately "ready" for everything
    # else. Folding this into `checks` (which `ready` is computed from) would make the
    # overall health check falsely report unready for that entirely normal situation.
    billing_status = {'paystack_configured': bool(_billing.PAYSTACK_SECRET), 'billing_enforced': _billing.BILLING_ENFORCE}
    return {'ready': all(checks.values()), 'checks': checks, 'providers': providers, 'billing': billing_status, 'moderation': moderation_status, 'notes': [
        'Billing (Paystack subscriptions + credit ledger) is implemented — see gateway/billing.py and the `billing` field above. Google Pay is TEST-mode only; production Google Pay is intentionally gated pending a confirmed production processor (see /api/billing/google-pay/status).',
        'Native Android rendering still requires a successful Gradle release build on a machine with the Android SDK/Gradle dependencies installed.',
        'Frontend/native crash telemetry is outside the gateway and must be verified on the target deployment platform.'
    ]}

class R2CleanupRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    uid: str = Field(min_length=1, max_length=128)

async def _cleanup_r2_user(uid: str) -> dict:
    """Delete every object under users/{uid}/ from R2 when R2 is configured."""
    endpoint = os.getenv('R2_ENDPOINT','')
    access_key = os.getenv('R2_ACCESS_KEY_ID','')
    secret_key = os.getenv('R2_SECRET_ACCESS_KEY','')
    bucket = os.getenv('R2_BUCKET','')
    if not all((endpoint, access_key, secret_key, bucket)):
        return {'configured': False, 'deleted': 0, 'failed': 0}
    try:
        import boto3
        from botocore.config import Config as BotoConfig
        s3 = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=access_key,
                          aws_secret_access_key=secret_key, config=BotoConfig(signature_version='s3v4'), region_name='auto')
    except ImportError:
        raise HTTPException(503, 'boto3 is not installed on the gateway.')
    prefix = f'users/{uid}/'
    deleted = failed = 0
    continuation = None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': prefix, 'MaxKeys': 1000}
        if continuation:
            kwargs['ContinuationToken'] = continuation
        page = await asyncio.to_thread(s3.list_objects_v2, **kwargs)
        keys = [x['Key'] for x in page.get('Contents', []) if x.get('Key')]
        if keys:
            for i in range(0, len(keys), 1000):
                batch = keys[i:i+1000]
                try:
                    await asyncio.to_thread(s3.delete_objects, Bucket=bucket, Delete={'Objects':[{'Key':k} for k in batch], 'Quiet':True})
                    deleted += len(batch)
                except Exception:
                    failed += len(batch)
        if not page.get('IsTruncated'):
            break
        continuation = page.get('NextContinuationToken')
        if not continuation:
            break
    return {'configured': True, 'deleted': deleted, 'failed': failed}

@app.post('/api/admin/storage/cleanup-user')
async def admin_cleanup_user_storage(req: R2CleanupRequest, request: Request, user=Depends(require_admin)):
    if req.uid == user.get('sub'):
        raise HTTPException(400, "Can't clean up your own admin storage from this endpoint.")
    result = await _cleanup_r2_user(req.uid)
    await _audit(user, 'cleanup_user_r2', {'uid': req.uid, **result})
    return {'uid': req.uid, **result}

class RemoteConfigRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=100, pattern=r'^[a-z][a-z0-9_]*$')
    value: Any
    description: str | None = Field(default=None, max_length=500)

@app.get('/api/admin/remote-config')
async def admin_list_remote_config(request: Request, _=Depends(require_admin)):
    return {'config': await persistence.list_remote_config()}

@app.post('/api/admin/remote-config')
async def admin_set_remote_config(req: RemoteConfigRequest, request: Request, user=Depends(require_admin)):
    result = await persistence.set_remote_config(req.key, req.value, req.description)
    await _audit(user, 'set_remote_config', {'key': req.key, 'value': req.value})
    log.info(f'Remote config updated: {req.key} = {req.value!r}')
    return {'config': result}

@app.get('/api/remote-config')
async def public_remote_config(request: Request, user=Depends(auth)):
    """Read-only for any signed-in user — same split as /api/flags vs /api/admin/flags."""
    if not user:
        raise HTTPException(401, 'Sign in required.')
    rows = await persistence.list_remote_config()
    return {'config': {r['key']: r['value'] for r in rows}}

class CreatePromptRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    prompt_key: str = Field(min_length=1, max_length=100, pattern=r'^[a-z][a-z0-9_]*$')
    content: str = Field(min_length=1, max_length=8000)

@app.get('/api/admin/prompts/{prompt_key}')
async def admin_list_prompts(prompt_key: str, request: Request, _=Depends(require_admin)):
    return {'versions': await persistence.list_prompt_versions(prompt_key)}

@app.post('/api/admin/prompts')
async def admin_create_prompt(req: CreatePromptRequest, request: Request, user=Depends(require_admin)):
    result = await persistence.create_prompt_version(req.prompt_key, req.content, user.get('sub'))
    await _audit(user, 'create_prompt_version', {'prompt_key': req.prompt_key, 'version': result.get('version')})
    log.info(f'New prompt version created: {req.prompt_key} v{result.get("version")}')
    return {'version': result}

class ActivatePromptRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: int = Field(gt=0)

@app.post('/api/admin/prompts/{prompt_key}/activate')
async def admin_activate_prompt(prompt_key: str, req: ActivatePromptRequest, request: Request, user=Depends(require_admin)):
    """This IS the rollback mechanism — activating an older version is exactly what a
    rollback is, not a separate feature. History is never deleted, just superseded."""
    try:
        result = await persistence.activate_prompt_version(prompt_key, req.version)
    except persistence.PersistenceError as e:
        raise HTTPException(404, str(e))
    await _audit(user, 'activate_prompt_version', {'prompt_key': prompt_key, 'version': req.version})
    log.warning(f'Prompt {prompt_key} rolled back/activated to v{req.version} by {user.get("sub")}')
    return {'activated': result}

class TestAIEditorRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    command: str = Field(min_length=1, max_length=1000)
    clips: list[dict[str, Any]] = Field(default_factory=list, max_length=200)

@app.post('/api/admin/ai-editor-test')
async def admin_test_ai_editor(req: TestAIEditorRequest, request: Request, _=Depends(require_admin)):
    """AI Editor Testing Lab. Shows what the sanitizer actually filters, which is the whole
    point — without this, there's no visibility into whether Groq is behaving (hallucinated
    clip ids, out-of-range values, wrong types) versus the sanitizer just quietly cleaning
    up after it every time in production."""
    from gateway.edit_ops import sanitize_ops
    known_ids={str(c.get('id')) for c in req.clips if c.get('id')}
    groq_result = await call_groq_edit_planner(req.command, req.clips)
    sanitized = sanitize_ops(groq_result['raw_operations'], known_ids) if groq_result['ok'] else []
    dropped_count = None
    if groq_result['ok'] and isinstance(groq_result['raw_operations'], list):
        dropped_count = len(groq_result['raw_operations']) - len(sanitized)
    return {
        'groq_called': groq_result['ok'] or groq_result['reason'] != 'GROQ_API_KEY not configured',
        'groq_ok': groq_result['ok'],
        'groq_failure_reason': groq_result['reason'],
        'raw_llm_response_text': groq_result['raw_response'],
        'raw_operations_before_sanitization': groq_result['raw_operations'],
        'sanitized_operations': sanitized,
        'operations_dropped_by_sanitizer': dropped_count,
    }

def deep_replace(obj,values):
 if isinstance(obj,dict): return {k:deep_replace(v,values) for k,v in obj.items()}
 if isinstance(obj,list): return [deep_replace(v,values) for v in obj]
 if isinstance(obj,str):
  for k,v in values.items(): obj=obj.replace('{{'+k+'}}',str(v))
 return obj

def load_memory():
 try:
  data=json.loads(MEMORY_FILE.read_text()) if MEMORY_FILE.exists() else []
  return data if isinstance(data,list) else []
 except Exception:return []
def save_memory(x):
 MEMORY_FILE.parent.mkdir(parents=True,exist_ok=True); MEMORY_FILE.write_text(json.dumps(x[-MAX_MEMORY:],indent=2),encoding='utf-8')

@app.get('/health')
def health(request:Request,_=Depends(auth)): return {'ok':True,'comfy':COMFY_URL,'workflow':WORKFLOW.exists(),'memory':len(load_memory()),'mode':'local-autonomous'}

@app.get('/healthz')
def healthz():
    # Unauthenticated liveness probe for Cloud Run/uptime monitoring. This endpoint is
    # deliberately limited to process health and exposes no configuration or user data.
    return {'ok': True}
@app.get('/api/memory')
def memory(request:Request,_=Depends(auth)): return {'items':load_memory()}
@app.post('/api/memory')
def add_memory(req:Memory,request:Request,_=Depends(auth)):
 data=load_memory(); data.append(req.model_dump()); save_memory(data); return {'ok':True,'count':len(data)}

async def _call_gemini(prompt: str, *, system: str, model: str|None=None, json_mode: bool=False) -> str:
    key=os.getenv('GEMINI_API_KEY','')
    if not key:
        raise HTTPException(503,'Gemini is not configured on the gateway.')
    chosen=(model or os.getenv('GEMINI_MODEL','gemini-2.5-flash')).strip()
    if not re.fullmatch(r'[A-Za-z0-9._:-]{1,128}', chosen):
        raise HTTPException(400,'Invalid Gemini model name.')
    body={
        'systemInstruction': {'parts':[{'text':system}]},
        'contents':[{'role':'user','parts':[{'text':prompt[:MAX_PROMPT]}]}],
        'generationConfig': ({'responseMimeType':'application/json'} if json_mode else {}),
    }
    url=f'https://generativelanguage.googleapis.com/v1beta/models/{chosen}:generateContent'
    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        try:
            r=await client.post(url, params={'key':key}, json=body)
        except httpx.HTTPError as e:
            raise HTTPException(502,f'Gemini request failed: {e}')
    if r.status_code>=400:
        raise HTTPException(502,f'Gemini request failed ({r.status_code}).')
    try:
        data=r.json()
        text=''.join(p.get('text','') for p in data['candidates'][0]['content']['parts'])
    except Exception:
        raise HTTPException(502,'Gemini returned an invalid response.')
    if not text.strip():
        raise HTTPException(502,'Gemini returned an empty response.')
    return text.strip()

@app.post('/api/gemini/analyze')
async def gemini_analyze(req:GeminiRequest,request:Request,_=Depends(auth)):
    system=('You are Vidigen AI Director. Analyze a media-generation request and return ONLY JSON '
            'with keys type,tags,riskFlags,summary,suggestedRatio,suggestedDuration,shotCount,camera,lighting,style. '
            'Do not invent policy violations. Keep tags short and useful.')
    raw=await _call_gemini(req.prompt,system=system,model=req.model,json_mode=True)
    try:
        data=json.loads(raw)
    except Exception:
        raise HTTPException(502,'Gemini returned non-JSON analysis.')
    if not isinstance(data,dict):
        raise HTTPException(502,'Gemini returned an invalid analysis object.')
    return {'analysis':data,'model':req.model or os.getenv('GEMINI_MODEL','gemini-2.5-flash')}

@app.post('/api/gemini/improve')
async def gemini_improve(req:GeminiRequest,request:Request,_=Depends(auth)):
    mode=req.mode or 'Text → Video'
    system=('You are an expert cinematic AI director. Rewrite the user request into one production-ready '
            f'prompt for {mode}. Preserve the user intent. Add subject consistency, composition, camera movement, '
            'lighting, environment, pacing and useful negative constraints. Return ONLY the rewritten prompt text.')
    text=await _call_gemini(req.prompt,system=system,model=req.model,json_mode=False)
    return {'prompt':text,'model':req.model or os.getenv('GEMINI_MODEL','gemini-2.5-flash')}

@app.post('/api/analyze')
async def analyze(req:Analyze,request:Request,_=Depends(auth)):
 system='Classify a creative media request. Return JSON only with keys type,tags,riskFlags,summary. type must be one of video,text-to-image,image-to-video,video-to-video,avatar,commercial,caption. Keep tags to at most 8 short strings. Do not provide credentials or instructions for abuse.'
 payload={'model':OLLAMA_MODEL,'stream':False,'format':'json','messages':[{'role':'system','content':system},{'role':'user','content':req.prompt}]}
 try:
  async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
   r=await client.post(f'{OLLAMA_URL}/api/chat',json=payload)
  if r.status_code<400:
   data=r.json(); raw=data.get('message',{}).get('content','{}'); parsed=json.loads(raw)
   if isinstance(parsed,dict): return parsed
 except Exception:
  pass
 # Deterministic fallback keeps the app functional without a local LLM.
 from gateway.src_fallback import classify
 return classify(req.prompt)


GROQ_EDIT_SYSTEM_PROMPT = (
    'You translate a video editor\'s natural-language instruction into a JSON list of timeline '
    'operations. Return ONLY a JSON object: {"operations": [...]}. Each operation must be one of: '
    '{"type":"delete","clipId":"..."}, {"type":"duplicate","clipId":"..."}, '
    '{"type":"split","clipId":"..."}, {"type":"trim","clipId":"...","trimStart":N,"trimEnd":N}, '
    '{"type":"speed","clipId":"...","value":N} (0.25-4), '
    '{"type":"volume","clipId":"...","value":N} (0-2, 1=100%). '
    'clipId MUST be one of the ids given below — never invent one. If the instruction is unclear '
    'or does not map to any operation, return {"operations": []}. Do not explain, just return JSON.'
)

async def call_groq_edit_planner(command: str, clips: list[dict]) -> dict:
    """Calls Groq and returns the RAW result — deliberately does not sanitize here. Used by
    both /api/edit-plan (which sanitizes before returning) and the admin AI Editor Testing
    Lab (which needs to show the raw, untrusted model output alongside the sanitized result,
    specifically so an admin can see what sanitize_ops() is actually filtering out — that's
    the entire point of the testing lab, so this function must not hide that from either
    caller by sanitizing too early.

    AI Model Control: model, temperature, and a fallback model are read from Supabase's
    remote_config (via the dashboard) when available, falling back to env vars / hardcoded
    defaults so a missing config entry never breaks generation."""
    groq_key=os.getenv('GROQ_API_KEY','')
    if not groq_key:
        return {'ok': False, 'reason': 'GROQ_API_KEY not configured', 'raw_operations': None, 'raw_response': None}

    config = {}
    try:
        if persistence.enabled():
            rows = await persistence.list_remote_config()
            config = {r['key']: r['value'] for r in rows}
    except Exception:
        pass  # A remote-config read failure falls back to defaults below, never blocks editing.

    model = config.get('groq_edit_model') or os.getenv('GROQ_EDIT_MODEL', 'llama-3.3-70b-versatile')
    temperature = config.get('groq_edit_temperature', 0)
    fallback_model = config.get('groq_edit_fallback_model')  # e.g. a smaller/cheaper model, optional
    active_prompt = await persistence.get_active_prompt('groq_edit_planner') or GROQ_EDIT_SYSTEM_PROMPT

    clip_summary=[{'id':c.get('id'),'title':c.get('title'),'track':c.get('track'),
                   'duration':c.get('duration'),'trimStart':c.get('trimStart'),'trimEnd':c.get('trimEnd')}
                  for c in clips]
    user_prompt=f'Clips: {json.dumps(clip_summary)}\nInstruction: {command}'

    async def _call(use_model: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=15) as c:
            return await c.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={'Authorization':f'Bearer {groq_key}','Content-Type':'application/json'},
                json={
                    'model': use_model,
                    'messages':[{'role':'system','content':active_prompt},{'role':'user','content':user_prompt}],
                    'response_format':{'type':'json_object'},
                    'temperature': temperature,
                    'max_tokens':500,
                },
            )

    try:
        resp = await _call(model)
        if resp.status_code>=400 and fallback_model and fallback_model != model:
            log.warning(f'Groq model {model} failed ({resp.status_code}); retrying with fallback {fallback_model}')
            resp = await _call(fallback_model)
        if resp.status_code>=400:
            return {'ok': False, 'reason': f'Groq returned {resp.status_code}', 'raw_operations': None, 'raw_response': None}
        content=resp.json()['choices'][0]['message']['content']
        parsed=json.loads(content)
        return {'ok': True, 'reason': None, 'raw_operations': parsed.get('operations'), 'raw_response': content}
    except Exception as e:
        return {'ok': False, 'reason': str(e), 'raw_operations': None, 'raw_response': None}

@app.post('/api/edit-plan')
async def edit_plan(req:EditPlan,request:Request,_=Depends(auth)):
    """Translate a natural-language editing request into safe timeline operations.
    Prefers Groq (fast, cheap LLM inference) when configured, for genuine natural-language
    understanding instead of keyword matching; falls back to the original keyword-based
    parser below if Groq isn't configured or its response is unusable. Either path's output
    is run through sanitize_ops() before being returned — the LLM's raw output is NEVER
    trusted directly, since a confused model can reference a clip id that doesn't exist or
    return a nonsensical operation type."""
    from gateway.edit_ops import sanitize_ops
    known_ids={str(c.get('id')) for c in req.clips if c.get('id')}

    if req.clips:
        groq_result = await call_groq_edit_planner(req.command, req.clips)
        if groq_result['ok']:
            ops=sanitize_ops(groq_result['raw_operations'],known_ids)
            return {'version':'1','operations':ops,'safe':True,'engine':'groq',
                    'message':f'{len(ops)} timeline operation(s) understood via natural-language editing.'}
        # Falls through to the deterministic keyword parser below — never a hard failure,
        # regardless of why Groq didn't return something usable.

    q=req.command.lower().strip()
    ops=[]
    clip=req.clips[0] if req.clips else None
    # Explicit clip IDs may be included in the command; otherwise operate on the first/selected client clip.
    for c in req.clips:
        title=str(c.get('title','')).lower()
        if title and title in q:
            clip=c; break
    if not clip:
        clip=req.clips[0] if req.clips else None
    if clip:
        cid=clip.get('id')
        if any(x in q for x in ('delete','remove')): ops.append({'type':'delete','clipId':cid})
        elif any(x in q for x in ('duplicate','copy')): ops.append({'type':'duplicate','clipId':cid})
        elif 'split' in q: ops.append({'type':'split','clipId':cid})
        else:
            m=re.search(r'(?:trim|cut).*?(?:to|at)\s*(\d+(?:\.\d+)?)\s*s',q)
            if m:
                end=float(m.group(1)); ops.append({'type':'trim','clipId':cid,'trimStart':0,'trimEnd':max(0.1,end)})
            m=re.search(r'(?:speed|playback).*?(\d+(?:\.\d+)?)\s*x',q)
            if m: ops.append({'type':'speed','clipId':cid,'value':max(.25,min(4,float(m.group(1))))})
            m=re.search(r'(?:volume|audio).*?(\d+)\s*%',q)
            if m: ops.append({'type':'volume','clipId':cid,'value':max(0,min(2,float(m.group(1))/100))})
    ops=sanitize_ops(ops,known_ids)
    return {'version':'1','operations':ops,'safe':True,'engine':'keyword-fallback','message':'Timeline operations generated by the constrained keyword editor (Groq not configured or unavailable).'}

@app.post('/api/export-plan')
async def export_plan(req:ExportPlan,request:Request,_=Depends(auth)):
    duration=round(sum(max(0,float(c.get('trimEnd',c.get('duration',5)))-float(c.get('trimStart',0))) for c in req.clips),2)
    return {'ok':True,'message':f'Render plan ready: {len(req.clips)} clips, {duration}s, {req.ratio}, {req.captionCount} caption segments, audio={req.hasAudio}. Connect a native FFmpeg/MediaCodec worker to produce the master file.','render':{'codec':'h264','audio':'aac','ratio':req.ratio,'duration':duration}}

@app.post('/api/captions')
async def captions(request:Request, file:UploadFile=File(...), _=Depends(auth)):
    """Real speech-to-text endpoint. Uses local faster-whisper by default; never fabricates captions."""
    if not file.filename:
        raise HTTPException(400,'Audio file is required.')
    content=await file.read(MAX_AUDIO_BYTES+1)
    if len(content)>MAX_AUDIO_BYTES:
        raise HTTPException(413,'Audio file is too large.')
    suffix=Path(file.filename).suffix.lower() or '.bin'
    tmp=Path('/tmp')/f'vidigen-{uuid.uuid4().hex}{suffix}'
    tmp.write_bytes(content)
    try:
        if CAPTION_PROVIDER != 'local':
            raise HTTPException(501,'Only the local caption provider is enabled in this build. Add a server-side provider adapter before enabling cloud transcription.')
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            raise HTTPException(503,'Real caption engine is not installed. Install faster-whisper and ensure ffmpeg is available on the gateway host.')
        cache_key=(WHISPER_MODEL,os.getenv('WHISPER_DEVICE','cpu'),os.getenv('WHISPER_COMPUTE_TYPE','int8'))
        model=WHISPER_CACHE.get(cache_key)
        if model is None:
            model=WhisperModel(cache_key[0],device=cache_key[1],compute_type=cache_key[2])
            WHISPER_CACHE[cache_key]=model
        segments,info=model.transcribe(str(tmp), vad_filter=True, word_timestamps=True, beam_size=int(os.getenv('WHISPER_BEAM_SIZE','5')))
        out=[]
        for seg in segments:
            text=(seg.text or '').strip()
            words=[]
            for w in (getattr(seg,'words',None) or []):
                wt=(getattr(w,'word','') or '').strip()
                if wt: words.append({'start':round(float(w.start),3),'end':round(float(w.end),3),'text':wt})
            out.append({'start':round(float(seg.start),3),'end':round(float(seg.end),3),'text':text,'words':words})
        if not out: raise HTTPException(422,'Speech engine produced no speech segments.')
        return {'provider':'local-faster-whisper','language':getattr(info,'language',None),'segments':out}
    finally:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass

@app.get('/api/captions/health')
async def captions_health(request:Request,_=Depends(auth)):
    import shutil
    whisper_ok=True
    try: import faster_whisper  # noqa: F401
    except Exception: whisper_ok=False
    return {'provider':CAPTION_PROVIDER,'whisper_installed':whisper_ok,'ffmpeg_available':bool(shutil.which('ffmpeg')),'model':WHISPER_MODEL,'device':os.getenv('WHISPER_DEVICE','cpu')}

class RemoveBackgroundRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    media_url: str = Field(min_length=1, max_length=2000)
    kind: str = Field(default='image', pattern='^(image|video)$')

class PhotoEnhanceRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    media_url: str = Field(min_length=1, max_length=2000)

@app.post('/api/photo-enhance')
async def photo_enhance(req: PhotoEnhanceRequest, request: Request, user=Depends(auth)):
    uid=(user or {}).get('sub')
    if not uid:
        raise HTTPException(401,'Signed-in user required.')
    from gateway.billing import enforce_free_daily_feature, refund_free_daily_feature
    daily=await enforce_free_daily_feature(uid,'photo_enhance')
    import tempfile
    from PIL import Image, ImageEnhance, ImageOps
    try:
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source'
            output=Path(tmp)/'enhanced.jpg'
            await _download_render_source(req.media_url,source)
            with Image.open(source) as img:
                img=ImageOps.exif_transpose(img).convert('RGB')
                img=ImageOps.autocontrast(img, cutoff=1)
                img=ImageEnhance.Sharpness(img).enhance(1.35)
                img=ImageEnhance.Contrast(img).enhance(1.08)
                img.save(output,'JPEG',quality=94,optimize=True)

            endpoint=os.getenv('R2_ENDPOINT','')
            access=os.getenv('R2_ACCESS_KEY_ID','')
            secret=os.getenv('R2_SECRET_ACCESS_KEY','')
            bucket=os.getenv('R2_BUCKET','')
            cdn=os.getenv('R2_PUBLIC_BASE_URL','').rstrip('/')
            if not all((endpoint,access,secret,bucket,cdn)):
                raise HTTPException(501,'R2 storage is required for photo enhancement output.')
            import boto3
            from botocore.config import Config as BotoConfig
            s3=boto3.client('s3',endpoint_url=endpoint,aws_access_key_id=access,aws_secret_access_key=secret,config=BotoConfig(signature_version='s3v4'),region_name='auto')
            key=f'users/{uid}/enhanced/{uuid.uuid4().hex}.jpg'
            s3.upload_file(str(output),bucket,key,ExtraArgs={'ContentType':'image/jpeg'})
            return {'status':'complete','output_url':f'{cdn}/{key}','feature':'photo_enhance'}
    except HTTPException:
        if daily.get('plan') == 'free':
            await refund_free_daily_feature(uid,'photo_enhance')
        raise
    except Exception as e:
        if daily.get('plan') == 'free':
            await refund_free_daily_feature(uid,'photo_enhance')
        raise HTTPException(422,f'Photo enhancement failed: {e}')

@app.post('/api/remove-background')
async def remove_background_endpoint(req: RemoveBackgroundRequest, request: Request, user=Depends(auth)):
    """Background removal — image or video. This is CapCut's most-used tool after
    captions, and until now the gateway had no equivalent at all."""
    from gateway import providers as _providers
    try:
        if req.kind == 'video':
            result = await _providers.remove_background_video(req.media_url)
        else:
            result = await _providers.remove_background(req.media_url)
    except _providers.ProviderError as e:
        raise HTTPException(502, str(e))
    if not result.output_url and result.status not in ('starting', 'processing'):
        raise HTTPException(502, 'Background removal did not return an output.')
    return {'status': result.status, 'output_url': result.output_url, 'job_id': result.job_id}

class R2PresignRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    object_key: str = Field(min_length=1, max_length=500)
    content_type: str = Field(default='application/octet-stream', max_length=200)

@app.post('/api/r2-presign')
async def r2_presign(req: R2PresignRequest, request: Request, user=Depends(auth)):
    """Signs a short-lived presigned R2 PUT URL so the client uploads media directly to
    Cloudflare (fast, and doesn't route large files through this gateway's own bandwidth).
    R2's access key/secret live ONLY in this function's environment — never in the client.
    Previously this was config-only (.env.example had the R2_* vars but nothing read them);
    this is the actual missing pipeline."""
    endpoint = os.getenv('R2_ENDPOINT', '')
    access_key = os.getenv('R2_ACCESS_KEY_ID', '')
    secret_key = os.getenv('R2_SECRET_ACCESS_KEY', '')
    bucket = os.getenv('R2_BUCKET', '')
    if not all([endpoint, access_key, secret_key, bucket]):
        raise HTTPException(501, 'Cloudflare R2 is not configured on this gateway (R2_ENDPOINT/R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY/R2_BUCKET).')

    uid = user.get('sub') if isinstance(user, dict) else None
    # Force every upload under the caller's own uid prefix, same reasoning as the Supabase
    # RLS policies: a client-supplied key alone can't be trusted to scope itself correctly.
    prefix = f'users/{uid}/' if uid else 'anonymous/'
    safe_key = prefix + re.sub(r'\.\.+', '.', req.object_key.lstrip('/'))

    try:
        import boto3
        from botocore.config import Config as BotoConfig
    except ImportError:
        raise HTTPException(503, 'boto3 is not installed on the gateway (add it to requirements.txt).')

    s3 = boto3.client(
        's3',
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=BotoConfig(signature_version='s3v4'),
        region_name='auto',
    )
    try:
        upload_url = s3.generate_presigned_url(
            'put_object',
            Params={'Bucket': bucket, 'Key': safe_key, 'ContentType': req.content_type},
            ExpiresIn=300,
        )
    except Exception as e:
        raise HTTPException(502, f'Could not sign upload URL: {e}')

    cdn_base = os.getenv('R2_PUBLIC_BASE_URL', '').rstrip('/')
    return {
        'upload_url': upload_url,
        'object_key': safe_key,
        'cdn_url': f'{cdn_base}/{safe_key}' if cdn_base else None,
    }

class AutoReframeRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    media_url: str = Field(min_length=1, max_length=2000)
    target_aspect_w: int = Field(gt=0, le=100)
    target_aspect_h: int = Field(gt=0, le=100)

@app.post('/api/auto-reframe')
async def auto_reframe(req: AutoReframeRequest, request: Request, user=Depends(auth)):
    """Subject-centered reframe to a target aspect ratio. Scope, stated plainly (see also
    gateway/reframe.py): this analyzes ONE representative frame's subject position and
    applies a single fixed crop to the whole clip — it is NOT per-frame tracking. A subject
    that moves far from where it starts will drift toward the crop edge over a long clip.
    That's an honest v1, not the full CapCut-style tracked auto-reframe."""
    import shutil, subprocess, tempfile
    if not shutil.which('ffmpeg'):
        raise HTTPException(503, 'ffmpeg is not installed on this gateway host.')
    token = os.getenv('REPLICATE_API_TOKEN', '')
    if not token:
        raise HTTPException(501, 'Replicate is not configured on the server (REPLICATE_API_TOKEN).')

    from gateway import providers as _providers
    from gateway.reframe import bbox_from_alpha, compute_crop_box
    from PIL import Image
    import io as _io

    with tempfile.TemporaryDirectory() as tmp:
        source_path = f'{tmp}/source.mp4'
        frame_path = f'{tmp}/frame.png'
        output_path = f'{tmp}/reframed.mp4'

        async with httpx.AsyncClient(timeout=120) as c:
            dl = await c.get(req.media_url)
            if dl.status_code >= 400:
                raise HTTPException(502, f'Could not fetch source media ({dl.status_code}).')
            Path(source_path).write_bytes(dl.content)

        # Grab one representative frame ~15% into the clip (skips a possible black/fade-in
        # opening frame that a t=0 grab would often catch).
        probe = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', source_path],
            capture_output=True, text=True,
        )
        try:
            duration = float(probe.stdout.strip())
        except ValueError:
            duration = 5.0
        sample_at = max(0.1, duration * 0.15)
        extract = subprocess.run(
            ['ffmpeg', '-y', '-ss', str(sample_at), '-i', source_path, '-frames:v', '1', frame_path],
            capture_output=True,
        )
        if extract.returncode != 0 or not Path(frame_path).exists():
            raise HTTPException(500, 'Could not extract a sample frame from the source video.')

        # Reuse the same rembg model already used for background removal — no need for a
        # second, separate subject-detection model just to get a foreground mask.
        frame_bytes = Path(frame_path).read_bytes()
        async with httpx.AsyncClient(timeout=90) as c:
            upload = await c.post(
                'https://api.replicate.com/v1/predictions',
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Prefer': 'wait=25'},
                json={'version': _providers.REMBG_IMAGE_VERSION, 'input': {
                    'image': 'data:image/png;base64,' + __import__('base64').b64encode(frame_bytes).decode(),
                }},
            )
        if upload.status_code >= 400:
            raise HTTPException(502, f'Subject-detection request rejected ({upload.status_code}).')
        prediction = await _providers._poll_replicate_prediction(upload.json(), token, timeout_seconds=60)
        mask_url = prediction.get('output') if isinstance(prediction.get('output'), str) else None
        if not mask_url:
            raise HTTPException(502, 'Subject detection did not return a result in time.')

        async with httpx.AsyncClient(timeout=60) as c:
            mask_resp = await c.get(mask_url)
        mask_image = Image.open(_io.BytesIO(mask_resp.content)).convert('RGBA')
        subject_bbox = bbox_from_alpha(mask_image)
        crop_x, crop_y, crop_w, crop_h = compute_crop_box(
            frame_size=mask_image.size,
            subject_bbox=subject_bbox,
            target_aspect_w=req.target_aspect_w,
            target_aspect_h=req.target_aspect_h,
        )

        crop_filter = f'crop={crop_w}:{crop_h}:{crop_x}:{crop_y}'
        render = subprocess.run(
            ['ffmpeg', '-y', '-i', source_path, '-vf', crop_filter, '-c:a', 'copy', output_path],
            capture_output=True,
        )
        if render.returncode != 0 or not Path(output_path).exists():
            raise HTTPException(500, f'ffmpeg crop failed: {render.stderr.decode(errors="ignore")[-500:]}')

        # Upload the result the same way r2-presign's caller normally would, but server-side
        # since this endpoint already has the file on disk — no reason to round-trip it back
        # to the client just to have the client PUT it straight back to R2.
        endpoint = os.getenv('R2_ENDPOINT', '')
        access_key = os.getenv('R2_ACCESS_KEY_ID', '')
        secret_key = os.getenv('R2_SECRET_ACCESS_KEY', '')
        bucket = os.getenv('R2_BUCKET', '')
        cdn_base = os.getenv('R2_PUBLIC_BASE_URL', '').rstrip('/')
        if not all([endpoint, access_key, secret_key, bucket, cdn_base]):
            raise HTTPException(501, 'Cloudflare R2 is not fully configured on this gateway.')
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError:
            raise HTTPException(503, 'boto3 is not installed on the gateway.')
        s3 = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=access_key,
                           aws_secret_access_key=secret_key, config=BotoConfig(signature_version='s3v4'), region_name='auto')
        uid = user.get('sub') if isinstance(user, dict) else 'anonymous'
        object_key = f'users/{uid}/reframed/{uuid.uuid4().hex}.mp4'
        try:
            s3.upload_file(output_path, bucket, object_key, ExtraArgs={'ContentType': 'video/mp4'})
        except Exception as e:
            raise HTTPException(502, f'Could not upload reframed video: {e}')

        return {
            'output_url': f'{cdn_base}/{object_key}',
            'crop_box': {'x': crop_x, 'y': crop_y, 'width': crop_w, 'height': crop_h},
            'subject_detected': subject_bbox is not None,
        }

@app.get('/api/providers')
async def providers(request:Request,_=Depends(auth)):
    from gateway.provider_registry import ProviderRegistry, ProviderSpec
    registry=ProviderRegistry([
        ProviderSpec('replicate','video', 'REPLICATE_API_TOKEN'),
        ProviderSpec('seedance','video', 'SEEDANCE_API_TOKEN'),
        ProviderSpec('runway','video', 'RUNWAY_API_TOKEN'),
        ProviderSpec('avatar-gateway','avatar', 'AVATAR_API_TOKEN'),
    ])
    return {'providers': registry.list(), 'production_policy': 'Only configured providers are eligible for execution.'}

async def _bill_generation(user: dict | None, req: Generate) -> dict:
    from gateway.billing import consume_credits, enforce_free_daily_feature
    if not user or user.get('role') == 'gateway' or not user.get('sub'):
        return {'charged': 0, 'bypassed': True}
    duration_seconds = int(str(req.duration).rstrip('s'))
    operation = 'image' if 'image' in req.mode.lower() else 'video'
    if operation == 'video' and 'avatar' in req.mode.lower():
        daily=await enforce_free_daily_feature(user['sub'],'avatar')
        if daily.get('plan') == 'free':
            return {'charged':0,'free_daily_feature':'avatar','daily_count':daily.get('count'),'daily_limit':daily.get('limit')}
    model = (req.model if req.model and req.model != 'auto' else os.getenv('REPLICATE_MODEL','default-video'))
    return await consume_credits(user['sub'], operation, model, duration_seconds, metadata={'mode':req.mode,'prompt_hash':__import__('hashlib').sha256(req.prompt.encode()).hexdigest()})


@app.post('/api/generate')
async def generate(req:Generate,request:Request,user=Depends(auth)):
    """Create a persistent generation job and route it to the selected real provider."""
    from gateway.moderation import moderate_prompt
    prompt_check=await moderate_prompt(req.prompt)
    if not prompt_check['safe']:
        log.warning(f'Blocked prompt from user {(user or {}).get("sub","anon")}: {prompt_check.get("reason")}')
        raise HTTPException(422, f'This prompt was flagged and cannot be generated: {prompt_check.get("reason") or "policy violation"}')
    requested=(getattr(req,'model',None) or 'auto').lower() if hasattr(req,'model') else 'auto'
    configured={
        'replicate': bool(os.getenv('REPLICATE_API_TOKEN')) and bool(os.getenv('REPLICATE_MODEL')),
        'seedance': bool(os.getenv('SEEDANCE_API_URL')) and bool(os.getenv('SEEDANCE_API_TOKEN')),
        'runway': bool(os.getenv('RUNWAY_API_URL')) and bool(os.getenv('RUNWAY_API_TOKEN')),
    }
    if requested == 'auto':
        priority=[x.strip().lower() for x in os.getenv('VIDIGEN_PROVIDER_PRIORITY','replicate,seedance,runway').split(',') if x.strip()]
        provider_name=next((name for name in priority if configured.get(name) and name in PROVIDERS), 'local')
    elif requested in ('replicate','seedance','runway'):
        if not configured.get(requested):
            raise HTTPException(503,f'{requested.title()} is not configured on the gateway.')
        provider_name=requested
    else:
        provider_name='local'
    user_id=(user or {}).get('sub') if user else None
    if req.idempotencyKey and user_id and persistence.enabled():
        # Return the existing job instead of billing/submitting again. Best-effort: a
        # concurrent retry racing the first request past this check can still create two
        # jobs (no unique constraint backs this yet — see idempotencyKey note in the
        # Generate model), but this closes the common case (client retry after a slow
        # response, double-click) without requiring a schema migration to ship.
        existing=await persistence.get_job_by_idempotency_key(user_id, req.idempotencyKey)
        if existing:
            log.info(f'Idempotent replay for user {user_id}, key {req.idempotencyKey}: returning existing job {existing["id"]}')
            existing_request=existing.get('request') or {}
            external_id=existing_request.get('_external_id')
            if existing['id'] not in JOB_CACHE and external_id:
                JOB_CACHE[existing['id']]={'provider':existing['provider'],'external_id':external_id,'user_id':user_id,'request':existing_request}
            return {'promptId':existing['id'],'provider':existing['provider'],'status':existing.get('status','processing'),'externalJobId':external_id,'idempotentReplay':True}
    request_payload=req.model_dump()
    if provider_name in PROVIDERS and provider_name != 'local':
        billing_receipt=await _bill_generation(user, req)
        request_payload['_billing']=billing_receipt
        provider=PROVIDERS[provider_name]
        job_id=None
        try:
            job_id=await persistence.create_job(user_id,None,provider_name,os.getenv('REPLICATE_MODEL','') if provider_name=='replicate' else provider_name,request_payload,req.idempotencyKey) if user_id and persistence.enabled() else None
        except Exception as e:
            # The unique (user_id, idempotency_key) index makes this race-safe: when a
            # concurrent retry loses the insert race, return the winner's job after refunding
            # only the losing request's provisional charge.
            existing = await persistence.get_job_by_idempotency_key(user_id, req.idempotencyKey) if user_id and req.idempotencyKey and persistence.enabled() else None
            if existing:
                try:
                    from gateway.billing import refund_credits, refund_free_daily_feature
                    billing=request_payload.get('_billing',{})
                    if user_id and billing.get('charged'):
                        await refund_credits(user_id,int(billing['charged']),'idempotency_race_refund',{'provider':provider_name})
                    if user_id and billing.get('free_daily_feature'):
                        await refund_free_daily_feature(user_id,billing['free_daily_feature'])
                except Exception:
                    log.exception('Credit/daily-usage refund failed after idempotency race')
                existing_request=existing.get('request') or {}
                external_id=existing_request.get('_external_id')
                if existing['id'] not in JOB_CACHE and external_id:
                    JOB_CACHE[existing['id']]={'provider':existing['provider'],'external_id':external_id,'user_id':user_id,'request':existing_request}
                return {'promptId':existing['id'],'provider':existing['provider'],'status':existing.get('status','processing'),'externalJobId':external_id,'idempotentReplay':True}
            try:
                from gateway.billing import refund_credits, refund_free_daily_feature
                billing=request_payload.get('_billing',{})
                if user_id and billing.get('charged'): await refund_credits(user_id,int(billing['charged']),'job_persistence_failure',{'provider':provider_name})
                if user_id and billing.get('free_daily_feature'): await refund_free_daily_feature(user_id,billing['free_daily_feature'])
            except Exception: log.exception('Credit/daily-usage refund failed after job persistence error')
            raise HTTPException(503,'Generation job could not be created.')
        try:
            result=await provider.submit({'input':request_payload.get('input',request_payload)})
        except ProviderError as e:
            try:
                from gateway.billing import refund_credits, refund_free_daily_feature
                billing=request_payload.get('_billing',{})
                if user_id and billing.get('charged'): await refund_credits(user_id,int(billing['charged']),'provider_failure',{'provider':provider_name})
                if user_id and billing.get('free_daily_feature'): await refund_free_daily_feature(user_id,billing['free_daily_feature'])
            except Exception: log.exception('Credit/daily-usage refund failed after provider error')
            if job_id and persistence.enabled(): await persistence.update_job(job_id,status='failed',error=str(e))
            raise HTTPException(503,str(e))
        except Exception as e:
            log.exception('Unexpected generation provider failure')
            try:
                from gateway.billing import refund_credits, refund_free_daily_feature
                billing=request_payload.get('_billing',{})
                if user_id and billing.get('charged'): await refund_credits(user_id,int(billing['charged']),'provider_unexpected_failure',{'provider':provider_name})
                if user_id and billing.get('free_daily_feature'): await refund_free_daily_feature(user_id,billing['free_daily_feature'])
            except Exception: log.exception('Credit/daily-usage refund failed after unexpected provider error')
            if job_id and persistence.enabled():
                try: await persistence.update_job(job_id,status='failed',error='Unexpected provider failure')
                except Exception: log.exception('Failed to mark generation job failed')
            raise HTTPException(503,'Generation provider request failed. Please try again.')
        external_id=result.job_id
        cache_id=job_id or external_id
        request_payload['_external_id']=external_id
        status_url=(result.raw or {}).get('status_url') or (result.raw or {}).get('statusUrl') or ((result.raw or {}).get('urls') or {}).get('get') if isinstance(result.raw,dict) else None
        if status_url: request_payload['_status_url']=status_url
        JOB_CACHE[cache_id]={'provider':provider_name,'external_id':external_id,'user_id':user_id,'request':request_payload}
        if job_id and persistence.enabled():
            try: await persistence.update_job(job_id,status='processing',request=request_payload)
            except Exception: log.exception('Generation job was submitted but persistence status update failed')
        return {'promptId':cache_id,'provider':provider_name,'status':result.status,'externalJobId':external_id}
    # Local ComfyUI path.
    if not WORKFLOW.exists(): raise HTTPException(503,'workflow_api.json is missing. Export an API-format workflow from ComfyUI.')
    billing_receipt=await _bill_generation(user, req)
    request_payload['_billing']=billing_receipt
    workflow=json.loads(WORKFLOW.read_text(encoding='utf-8'))
    duration=int(req.duration[:-1]); width,height={'16:9':(1024,576),'9:16':(576,1024),'1:1':(1024,1024),'4:5':(896,1120),'21:9':(1344,576)}[req.ratio]
    values={'PROMPT':req.prompt,'NEGATIVE_PROMPT':'low quality, watermark, distorted motion, flicker, broken anatomy','WIDTH':width,'HEIGHT':height,'DURATION':duration,'SEED':uuid.uuid4().int % 2147483647}
    workflow=deep_replace(workflow,values)
    async with httpx.AsyncClient(timeout=30,follow_redirects=False) as client:
        try:r=await client.post(f'{COMFY_URL}/prompt',json={'prompt':workflow,'client_id':'vidigen-local'})
        except httpx.HTTPError as e:
            if user_id and request_payload.get('_billing',{}).get('free_daily_feature'):
                from gateway.billing import refund_free_daily_feature
                await refund_free_daily_feature(user_id,request_payload['_billing']['free_daily_feature'])
            raise HTTPException(503,f'ComfyUI unavailable: {e}')
    if r.status_code>=400: raise HTTPException(r.status_code,'ComfyUI rejected the workflow.')
    data=r.json(); pid=data.get('prompt_id')
    if not pid or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',str(pid)): raise HTTPException(502,'Invalid prompt ID from ComfyUI')
    request_payload['_external_id']=pid
    JOB_CACHE[pid]={'provider':'local','external_id':pid,'user_id':user_id,'request':request_payload}
    job_id=await persistence.create_job(user_id,None,'local','comfyui',request_payload) if user_id and persistence.enabled() else None
    if job_id: JOB_CACHE[job_id]=JOB_CACHE[pid]; JOB_CACHE[job_id]['external_id']=pid; await persistence.update_job(job_id,status='processing',request=request_payload)
    return {'promptId':job_id or pid,'provider':'local','status':'processing'}

async def _auto_learn_output(user: dict | None, prompt_id: str, output_url: str, provider: str):
    if os.getenv('VIDIGEN_AUTO_LEARN_OUTPUTS','true').lower() not in {'1','true','yes','on'}: return
    uid=(user or {}).get('sub') if isinstance(user,dict) else None
    if not uid or prompt_id in LEARNED_OUTPUTS: return
    try:
        from gateway.brain_learning import LearnOutputRequest, learn_output
        LEARNED_OUTPUTS.add(prompt_id)
        goal=(JOB_CACHE.get(prompt_id) or {}).get('request',{}).get('prompt','')
        req_tags=(JOB_CACHE.get(prompt_id) or {}).get('request',{}).get('tags') or []
        tags=[str(x)[:40] for x in req_tags if isinstance(x,str)][:30] or [provider]
        await learn_output(LearnOutputRequest(output_url=output_url,source_prompt=goal,rating=0,accepted=False,tags=tags),uid)
        log.info(f'Brain auto-learned output for job {prompt_id}')
    except Exception:
        LEARNED_OUTPUTS.discard(prompt_id)
        log.exception(f'Brain auto-learning failed for job {prompt_id}')

@app.get('/api/status/{prompt_id}')
async def status(prompt_id:str,request:Request,user=Depends(auth)):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',prompt_id): raise HTTPException(400,'Invalid prompt ID')
    job=JOB_CACHE.get(prompt_id)
    if not job and persistence.enabled() and user and user.get('sub'):
        row=await persistence.get_job(prompt_id,user['sub'])
        if row: job={'provider':row['provider'],'external_id':row.get('request',{}).get('_external_id'),'user_id':user['sub'],'request':row.get('request',{})}
    if not job: raise HTTPException(404,'Generation job not found')
    caller_uid=(user or {}).get('sub') if isinstance(user,dict) else None
    owner_uid=job.get('user_id')
    if caller_uid and owner_uid and caller_uid != owner_uid:
        raise HTTPException(403,'You do not have access to this generation job.')
    provider=job['provider']; external=job['external_id']
    if provider in PROVIDERS:
        try:
            result=await PROVIDERS[provider].status(external,(job.get('request') or {}).get('_status_url'))
            if result.status.lower() in ('succeeded','completed','successful','complete') and result.output_url:
                # Output moderation happens ONCE per job, cached on the job record — not
                # re-run on every poll a client makes while waiting, which would otherwise
                # mean repeated Replicate charges and repeated ffmpeg frame extraction for
                # the same single output.
                mod_result=job.get('_moderation')
                if mod_result is None:
                    from gateway.moderation import moderate_output_video, moderate_output_image
                    op=(job.get('request') or {}).get('mode','')
                    mod_result=await (moderate_output_image(result.output_url) if 'image' in str(op).lower() else moderate_output_video(result.output_url))
                    job['_moderation']=mod_result
                    JOB_CACHE[prompt_id]=job
                if not mod_result.get('safe'):
                    # Blocks both on an actual classifier hit (checked=True) and on
                    # "moderation could not run and enforcement requires a check"
                    # (checked=False, safe=False) — see gateway/moderation.py's
                    # VIDIGEN_MODERATION_ENFORCE policy. checked still distinguishes the two
                    # for logging/audit even though both now block delivery.
                    log.warning(f'Blocked output for job {prompt_id}, user {(user or {}).get("sub","anon")}: {mod_result.get("reason")}')
                    if persistence.enabled() and user and user.get('sub'):
                        billing_info=(job.get('request') or {}).get('_billing') or {}
                        charged=int(billing_info.get('charged') or 0)
                        if charged:
                            from gateway.billing import refund_credits
                            await refund_credits(user['sub'],charged,'moderation_blocked',{'job_id':prompt_id})
                        await persistence.update_job(prompt_id,status='blocked',error='Output failed content moderation.')
                    return {'status':'error','error':'This generation was blocked by content moderation. Any credits charged have been refunded.'}
                if persistence.enabled() and user and user.get('sub'): await persistence.update_job(prompt_id,status='completed',completed_at=time.strftime('%Y-%m-%dT%H:%M:%SZ'))
                if user and user.get('sub'): asyncio.create_task(_auto_learn_output(user,prompt_id,result.output_url,provider))
                return {'status':'complete','videoUrl':result.output_url,'provider':provider}
            if result.status.lower() in ('failed','canceled','cancelled','error'):
                if persistence.enabled() and user and user.get('sub'): await persistence.update_job(prompt_id,status='failed',error=str((result.raw or {}).get('error','Provider failed')))
                return {'status':'error','error':str((result.raw or {}).get('error','Provider failed'))}
            return {'status':'running','provider':provider}
        except ProviderError as e: raise HTTPException(502,str(e))
    async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
        try:r=await client.get(f'{COMFY_URL}/history/{external}')
        except httpx.HTTPError as e: raise HTTPException(503,f'ComfyUI unavailable: {e}')
    if r.status_code==404:return {'status':'running'}
    if r.status_code>=400:raise HTTPException(r.status_code,'ComfyUI history request failed')
    history=r.json().get(external)
    if not history:return {'status':'running'}
    if history.get('status',{}).get('status_str')=='error':return {'status':'error','error':'ComfyUI reported a generation error'}
    for node in history.get('outputs',{}).values():
        if not isinstance(node,dict): continue
        for key in ('gifs','videos','images'):
            for item in node.get(key,[]) if isinstance(node.get(key,[]),list) else []:
                if not isinstance(item,dict): continue
                filename=item.get('filename')
                if filename and (key in ('gifs','videos') or str(filename).lower().endswith(('.mp4','.webm','.gif'))):
                    cap=secrets.token_urlsafe(32); OUTPUT_CAPS[cap]={'filename':filename,'subfolder':item.get('subfolder',''),'type':item.get('type','output'),'expires':time.time()+OUTPUT_CAP_TTL}
                    if persistence.enabled() and user and user.get('sub'):
                        local_url=str(request.base_url).rstrip('/')+f'/api/output?cap={cap}'
                        await persistence.update_job(prompt_id,status='completed',completed_at=time.strftime('%Y-%m-%dT%H:%M:%SZ'))
                        asyncio.create_task(_auto_learn_output(user,prompt_id,local_url,'local'))
                    return {'status':'complete','videoUrl':str(request.base_url).rstrip('/')+f'/api/output?cap={cap}'}
    return {'status':'running'}

class Feedback(BaseModel):
    model_config=ConfigDict(extra='forbid')
    job_id:str=Field(min_length=1,max_length=128)
    rating:int=Field(ge=0,le=5)
    accepted:bool=False
    signals:dict[str,Any]=Field(default_factory=dict)

@app.post('/api/feedback')
async def feedback(req:Feedback,request:Request,user=Depends(auth)):
    if not user or not user.get('sub') or not persistence.enabled():
        raise HTTPException(503,'Supabase persistence is required for server-side Brain feedback.')
    owned_job=await persistence.get_job(req.job_id,user['sub'])
    if not owned_job:
        raise HTTPException(404,'Generation job not found for this account.')
    signals=dict(req.signals or {})
    if not signals.get('tags'):
        signals['tags']=list((owned_job.get('request') or {}).get('tags') or [])
    await persistence.add_feedback(user['sub'],req.job_id,req.rating,req.accepted,signals)
    return {'ok':True}

@app.get('/api/output')
async def output(cap:str):
 ref=OUTPUT_CAPS.get(cap)
 if not ref or ref['expires']<time.time():
  OUTPUT_CAPS.pop(cap,None); raise HTTPException(404,'Output link expired or invalid')
 async with httpx.AsyncClient(timeout=60,follow_redirects=False) as client:
  try:r=await client.get(f'{COMFY_URL}/view',params={'filename':ref['filename'],'subfolder':ref['subfolder'],'type':ref['type']})
  except httpx.HTTPError as e: raise HTTPException(503,f'ComfyUI unavailable: {e}')
 if r.status_code>=400: raise HTTPException(r.status_code,'Output unavailable')
 return Response(content=r.content,media_type=r.headers.get('content-type','application/octet-stream'))

# --- Billing / subscriptions / Brain learning -----------------------------------------
from gateway.billing import build_router as build_billing_router
from gateway.brain_learning import build_router as build_brain_router
app.include_router(build_billing_router(auth, require_admin))
app.include_router(build_brain_router(auth))

# --- Vidigen AI Agent Orchestrator -------------------------------------------------------
# Mounted after the canonical V12 services are defined so the agent delegates to those
# same authenticated provider/generation contracts rather than creating a second stack.
from gateway.agent.api import create_agent_router
app.include_router(create_agent_router(auth))

from gateway.backup import build_router as build_backup_router
app.include_router(build_backup_router(require_admin))
