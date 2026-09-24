from __future__ import annotations
import asyncio, json, os, shutil, subprocess, tempfile, urllib.parse
from pathlib import Path
from typing import Any
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from PIL import Image, ImageStat
from gateway import persistence

class LearnOutputRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    output_url: str = Field(min_length=8,max_length=4000)
    source_prompt: str = Field(default='',max_length=4000)
    rating: int = Field(default=0,ge=0,le=5)
    accepted: bool = False
    tags: list[str] = Field(default_factory=list,max_length=30)


def _allowed_host(url: str) -> bool:
    p=urllib.parse.urlparse(url)
    if p.scheme not in ('https','http') or not p.hostname: return False
    configured=os.getenv('VIDIGEN_LEARNING_ALLOWED_HOSTS') or os.getenv('VIDIGEN_RENDER_ALLOWED_HOSTS','')
    allowed={x.strip().lower().rstrip('.') for x in configured.split(',') if x.strip()}
    base=os.getenv('R2_PUBLIC_BASE_URL','')
    try:
        hostname=urllib.parse.urlparse(base).hostname
        if hostname: allowed.add(hostname.lower().rstrip('.'))
    except Exception:
        pass
    return p.hostname.lower().rstrip('.') in allowed

async def _download(url: str, path: str):
    if not _allowed_host(url): raise HTTPException(403,'Learning source host is not allowlisted.')
    async with httpx.AsyncClient(timeout=180,follow_redirects=False) as c:
        async with c.stream('GET',url) as r:
            if r.status_code>=400: raise HTTPException(502,'Could not fetch learning output.')
            with open(path,'wb') as f:
                total=0
                async for chunk in r.aiter_bytes(1024*1024):
                    total+=len(chunk)
                    if total>500_000_000: raise HTTPException(413,'Learning source exceeds 500MB.')
                    f.write(chunk)

def _sample_video(path: str, outdir: str) -> tuple[dict,list[dict]]:
    if not shutil.which('ffprobe') or not shutil.which('ffmpeg'):
        raise HTTPException(503,'ffmpeg and ffprobe are required for output learning.')
    probe=subprocess.run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',path],capture_output=True,text=True,timeout=60)
    if probe.returncode!=0: raise HTTPException(422,'Generated video could not be decoded.')
    meta=json.loads(probe.stdout or '{}'); streams=meta.get('streams') or []; video=next((s for s in streams if s.get('codec_type')=='video'),None)
    if not video: raise HTTPException(422,'Learning output does not contain a video stream.')
    duration=float((meta.get('format') or {}).get('duration') or video.get('duration') or 0)
    width=int(video.get('width') or 0); height=int(video.get('height') or 0); fps=video.get('r_frame_rate','0/1')
    count=max(3,min(12,int(duration//2) if duration else 3))
    fps_expr=max(0.1,min(2.0,count/max(duration,1)))
    pattern=str(Path(outdir)/'frame-%02d.jpg')
    r=subprocess.run(['ffmpeg','-y','-i',path,'-vf',f'fps={fps_expr}', '-frames:v',str(count),pattern],capture_output=True,text=True,timeout=120)
    if r.returncode!=0: raise HTTPException(422,'Could not sample generated video frames.')
    samples=[]
    for i,p in enumerate(sorted(Path(outdir).glob('frame-*.jpg'))):
        img=Image.open(p).convert('RGB'); stat=ImageStat.Stat(img)
        samples.append({'index':i,'path':str(p),'width':img.width,'height':img.height,'mean_rgb':[round(v,2) for v in stat.mean],'brightness':round(sum(stat.mean)/3,2)})
    return {'media_type':'video','duration':duration,'width':width,'height':height,'fps':fps,'sample_count':len(samples)},samples

def _sample_image(path: str, outdir: str) -> tuple[dict,list[dict]]:
    img=Image.open(path).convert('RGB'); stat=ImageStat.Stat(img)
    return {'media_type':'image','width':img.width,'height':img.height,'sample_count':1},[{'index':0,'path':path,'width':img.width,'height':img.height,'mean_rgb':[round(v,2) for v in stat.mean],'brightness':round(sum(stat.mean)/3,2)}]

async def learn_output(req: LearnOutputRequest, user_id: str) -> dict:
    if not persistence.enabled(): raise HTTPException(503,'Supabase persistence is required for production Brain learning.')
    with tempfile.TemporaryDirectory(prefix='vidigen-brain-') as tmp:
        src=Path(tmp)/'source.bin'; await _download(req.output_url,str(src))
        kind='video'
        try:
            meta,samples=_sample_video(str(src),tmp)
        except HTTPException:
            try: meta,samples=_sample_image(str(src),tmp); kind='image'
            except Exception as exc: raise HTTPException(422,f'Output analysis failed: {exc}')
        pattern={'source_prompt':req.source_prompt,'tags':req.tags,'rating':req.rating,'accepted':req.accepted,'media':{k:v for k,v in meta.items() if k!='media_type'},'signals':{'brightness_mean':round(sum(s['brightness'] for s in samples)/max(len(samples),1),2)}}
        created=await persistence.sb_request('POST','brain_output_samples',{'user_id':user_id,'output_url':req.output_url,'media_type':kind,'analysis':meta,'rating':req.rating,'accepted':req.accepted,'source_prompt':req.source_prompt,'tags':req.tags})
        # Store compact learnable pattern separately; no automatic weight updates or opaque model training occurs here.
        await persistence.sb_request('POST','brain_patterns',{'user_id':user_id,'pattern_type':kind,'pattern':pattern,'source_sample_id':created[0]['id'] if created else None})
        return {'ok':True,'media':meta,'samples':samples,'pattern':pattern,'learning_mode':'retrieval_and_adaptation'}


def build_router(auth_dependency):
    router=APIRouter(prefix='/api/brain',tags=['brain'])
    @router.post('/learn-output')
    async def learn(req: LearnOutputRequest,user=Depends(auth_dependency)):
        uid=(user or {}).get('sub') if isinstance(user,dict) else None
        if not uid: raise HTTPException(401,'Signed-in user required.')
        return await learn_output(req,uid)
    @router.get('/profile')
    async def profile(user=Depends(auth_dependency)):
        uid=(user or {}).get('sub') if isinstance(user,dict) else None
        if not uid: raise HTTPException(401,'Signed-in user required.')
        return await persistence.get_brain_profile(uid)

    @router.get('/patterns')
    async def patterns(user=Depends(auth_dependency)):
        uid=(user or {}).get('sub') if isinstance(user,dict) else None
        if not uid or not persistence.enabled(): raise HTTPException(401,'Signed-in user required.')
        rows=await persistence.sb_request('GET','brain_patterns',params={'user_id':f'eq.{uid}','select':'*','order':'created_at.desc','limit':'100'})
        return {'patterns':rows or []}
    return router
