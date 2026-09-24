import React, {useEffect, useMemo, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import * as Sentry from '@sentry/react';
import './styles.css';
import {buildPreferenceProfile, buildLocalPrompt, normalizeRequest, validateMemoryRecord} from './learning.mjs';
import {analyzeWithGemini, improvePromptWithGemini, geminiConfigured} from './gemini.js';
import {nativeExportAvailable, exportProjectNative, copyFileToNativeStorage} from './nativeRenderEngine.js';
import CustomerAuthScreen from './CustomerAuthScreen.jsx';
import {supabase, supabaseConfigured} from './supabaseClient.js';

// Frontend error monitoring — the other half of Sentry-based Error Monitoring (the backend
// half lives in gateway/server.py's global exception handlers). Guarded by an env var so
// this is a genuine no-op with zero network calls when unset, not a silent failure.
const SENTRY_DSN = import.meta.env.VITE_SENTRY_DSN || '';
if (SENTRY_DSN) {
  Sentry.init({
    dsn: SENTRY_DSN,
    environment: import.meta.env.MODE || 'production',
    tracesSampleRate: 0.1,
    // Deliberately no session replay / no PII capture by default — this app handles
    // generated media and auth tokens; defaulting to more telemetry than necessary is the
    // wrong tradeoff for a video-generation tool.
  });
}

const BUILD_HEALTH_CHECK='cloud-run-direct';
const NAV=[['Create','✦'],['Media','▧'],['Text','T'],['Effects','◌'],['Audio','♫'],['Captions','CC']];
const MODES=['Text → Video','Image → Video','Video → Video','Text → Image'];
const CAMERA_MOVES=['Auto (let the model choose)','Static shot','Slow push in','Pull out','Pan left','Pan right','Tilt up','Tilt down','Orbit around subject','Handheld','Aerial / drone','Dolly tracking shot'];
const RATIOS=['16:9','9:16','1:1','4:5','21:9'];
const DURATIONS=['4s','5s','8s','10s','15s','30s','60s'];

const DEFAULT_CLIP={trimStart:0,trimEnd:5,speed:1,volume:1,brightness:100,contrast:100,saturation:100,blur:0,rotation:0,scale:100,opacity:100,keyframes:[]};
const read=(k,f)=>{try{return JSON.parse(localStorage.getItem(k)||'null')??f}catch{return f}};
const save=(k,v)=>localStorage.setItem(k,JSON.stringify(v));

let _onUnauthorizedHandler = null;
const DEFAULT_GATEWAY_FALLBACK='https://api.vidigen.online';
const CANONICAL_GATEWAY_FALLBACK='https://vidigen-gateway-xvpegaghzq-uc.a.run.app';
async function gatewayFetch(base,path,options={},token=''){
 const hasBody=options.body!=null;
 const headers={...((hasBody && !(options.body instanceof FormData))?{'Content-Type':'application/json'}:{}),...(options.headers||{})};
 if(token) headers.Authorization=`Bearer ${token}`;
 const primary=base.replace(/\/$/,'');
 const fallback=CANONICAL_GATEWAY_FALLBACK.replace(/\/$/,'');
 const cloudRunFallback=fallback;
 let r;
 try{
   r=await fetch(`${primary}${path}`,{...options,headers});
 }catch(primaryError){
   if(primary!==fallback){
     r=await fetch(`${fallback}${path}`,{...options,headers});
     base=fallback;
   }else throw primaryError;
 }
 // Only treat this as "your session expired" when a token was actually sent and rejected —
 // not for calls made with no token at all (some dev/local-gateway paths are intentionally
 // unauthenticated), and guarded against firing repeatedly for every in-flight request when
 // a session has already expired, not just the first one to notice.
 if(r.status===401 && token && _onUnauthorizedHandler) _onUnauthorizedHandler();
 if(!r.ok){
   // If the configured API edge is stale/unreachable at the HTTP layer, retry the
   // canonical Cloud Run gateway before reporting the app offline.
   if(primary!==fallback && (r.status>=500 || r.status===404)){
     try{
       const retry=await fetch(`${fallback}${path}`,{...options,headers});
       if(retry.ok){ r=retry; base=fallback; } else if(fallback!==cloudRunFallback){
         const retry2=await fetch(`${cloudRunFallback}${path}`,{...options,headers});
         if(retry2.ok){ r=retry2; base=cloudRunFallback; }
       }
     }catch{}
   }
   // A stale/unverified custom API hostname should not strand the live frontend while the
   // canonical Cloud Run HTTPS endpoint is healthy. 404 is included because an unverified
   // Google/Cloudflare custom-domain mapping can return a front-door 404 before the request
   // reaches FastAPI. Authentication failures remain authoritative on the primary host.
   const shouldFallback = primary!==fallback && (r.status===404 || r.status>=500);
   if(shouldFallback){
     r=await fetch(`${fallback}${path}`,{...options,headers});
     base=fallback;
     if(!r.ok && fallback!==cloudRunFallback){
       const retry2=await fetch(`${cloudRunFallback}${path}`,{...options,headers});
       if(retry2.ok){ r=retry2; base=cloudRunFallback; }
     }
   }
 }
 if(!r.ok){let m=`Gateway HTTP ${r.status}`;try{const j=await r.json();m=j.detail||j.error||m}catch{}throw new Error(m)}
 return r;
}
async function waitJob(base,token,id,onProgress){
 for(let i=0;i<450;i++){
  await new Promise(x=>setTimeout(x,2000));
  const st=await (await gatewayFetch(base,`/api/status/${encodeURIComponent(id)}`,{},token)).json();
  onProgress?.(st);
  if(st.videoUrl||st.outputUrl)return {url:st.videoUrl||st.outputUrl,jobId:id};
  if(st.status==='error')throw new Error(st.error||'Generation failed.');
 }
 throw new Error('Generation timed out.');
}
async function generateScene(base,token,payload,onProgress){
 // idempotencyKey lets a retried request (network blip, client retry) land on the
 // gateway's existing-job lookup instead of billing and submitting a second time — see
 // gateway/server.py's /api/generate handler. Generated once per logical generation call
 // (not per HTTP attempt), so an actual retry of the same call reuses the same key.
 const withKey={...payload,idempotencyKey:(payload.idempotencyKey||(crypto?.randomUUID?crypto.randomUUID():`${Date.now()}-${Math.random().toString(36).slice(2)}`))};
 const data=await (await gatewayFetch(base,'/api/generate',{method:'POST',body:JSON.stringify(withKey)},token)).json();
 if(data.videoUrl||data.outputUrl)return {url:data.videoUrl||data.outputUrl,jobId:data.promptId};
 if(!data.promptId)throw new Error('No generation job ID returned.');
 return waitJob(base,token,data.promptId,onProgress);
}

async function loadBillingData(base,token){
 const [plansRes,meRes]=await Promise.all([gatewayFetch(base,'/api/billing/plans',{},token),gatewayFetch(base,'/api/billing/me',{},token)]);
 return {plans:(await plansRes.json()).plans||[], ...(await meRes.json())};
}
async function startBillingCheckout(base,token,slug,payment_method='paystack'){
 const r=await gatewayFetch(base,'/api/billing/checkout/paystack',{method:'POST',body:JSON.stringify({plan_slug:slug,payment_method})},token);
 const d=await r.json(); if(!d.authorization_url) throw new Error('No Paystack checkout URL returned.'); window.location.href=d.authorization_url;
}

function PasswordResetScreen({onClose,onDone}) {
 const [password,setPassword]=useState('');
 const [confirm,setConfirm]=useState('');
 const [busy,setBusy]=useState(false);
 const [error,setError]=useState('');
 const [info,setInfo]=useState('');
 async function savePassword(e){
   e.preventDefault(); setError(''); setInfo('');
   if(password.length<8){setError('Password must be at least 8 characters.');return}
   if(password!==confirm){setError('Passwords do not match.');return}
   setBusy(true);
   try{
     const {error:err}=await supabase.auth.updateUser({password});
     if(err) throw err;
     setInfo('Password updated successfully. You can now sign in with your new password.');
     setTimeout(()=>onDone(),500);
   }catch(err){setError(err.message||'Could not update password.')}
   finally{setBusy(false)}
 }
 return <div className="modalBack"><div className="modal">
   <div className="modalHead"><b>Set a new password</b><button className="secondary" type="button" onClick={onClose}>×</button></div>
   <p className="muted">Choose a new password for your Vidigen account.</p>
   <form onSubmit={savePassword}>
     <label>New password</label>
     <input type="password" minLength={8} required value={password} onChange={e=>setPassword(e.target.value)} autoComplete="new-password"/>
     <label>Confirm password</label>
     <input type="password" minLength={8} required value={confirm} onChange={e=>setConfirm(e.target.value)} autoComplete="new-password"/>
     {error&&<p style={{color:'var(--bad,#f87171)'}}>{error}</p>}
     {info&&<p className="muted">{info}</p>}
     <button className="primary" type="submit" disabled={busy} style={{marginTop:12}}>{busy?'Saving…':'Set password'}</button>
   </form>
 </div></div>
}

function App(){
 const [nav,setNav]=useState('Create');
 const [mode,setMode]=useState('Text → Video');
 const [cameraMove,setCameraMove]=useState(CAMERA_MOVES[0]);
 const [prompt,setPrompt]=useState('Create a cinematic 30-second product advertisement for a premium sneaker, luxury studio, controlled camera movement and a strong final CTA.');
 const [ratio,setRatio]=useState('16:9'),[duration,setDuration]=useState('5s'),[model,setModel]=useState('auto');
 const [gateway,setGateway]=useState(()=>CANONICAL_GATEWAY_FALLBACK);
 const [token,setToken]=useState(()=>sessionStorage.getItem('vidigen_gateway_token')||'');
 const [showAuth,setShowAuth]=useState(false);
 const [showPasswordReset,setShowPasswordReset]=useState(false);
 const [online,setOnline]=useState(false),[providerInfo,setProviderInfo]=useState(null),[status,setStatus]=useState('Ready'),[progress,setProgress]=useState(0);
 const [billing,setBilling]=useState(null),[billingBusy,setBillingBusy]=useState(false);
 const [generating,setGenerating]=useState(false),[history,setHistory]=useState(()=>read('vidigen_learning_history',[]));
 const [projects,setProjects]=useState(()=>read('vidigen_projects',[]));
 const [clips,setClips]=useState(()=>read('vidigen_timeline_v12',[]));
 const [activeId,setActiveId]=useState(()=>read('vidigen_active_clip',null));
 const [memoryOn,setMemoryOn]=useState(()=>localStorage.getItem('vidigen_learning')!=='off');
 const [analysis,setAnalysis]=useState(null),[showBrain,setShowBrain]=useState(false),[showSettings,setShowSettings]=useState(false),[settingsTab,setSettingsTab]=useState('general');
 const [brainProfile,setBrainProfile]=useState({preferred_tags:[],successful_prompts:[],feedback_count:0,learned_outputs:0});
 const [showExport,setShowExport]=useState(false),[exportBusy,setExportBusy]=useState(false),[zoom,setZoom]=useState(1),[bgRemoving,setBgRemoving]=useState(false),[reframing,setReframing]=useState(false),[previewTime,setPreviewTime]=useState(0);
 const [generationSource,setGenerationSource]=useState(null);
 const [undoStack,setUndoStack]=useState([]),[redoStack,setRedoStack]=useState([]);
 const [captions,setCaptions]=useState(()=>read('vidigen_captions',[])),[captioning,setCaptioning]=useState(false),[captionStyle,setCaptionStyle]=useState('Bold');
 const [audio,setAudio]=useState(()=>read('vidigen_audio',null)),[recording,setRecording]=useState(false);
 const [overlay,setOverlay]=useState({text:'',size:42,x:50,y:82,bold:true});
 const [avatar,setAvatar]=useState({script:'Welcome to Vidigen AI. Turn an idea into a polished video in minutes.',presenter:'Studio Presenter',voice:'Natural',language:'English',background:'Studio'});
 const [aiCommand,setAiCommand]=useState('');
 const [commandBusy,setCommandBusy]=useState(false);
 const video=useRef(null),audioInput=useRef(null),recorder=useRef(null),chunks=useRef([]);
 const profile=useMemo(()=>buildPreferenceProfile(history),[history]);
 const activeClip=clips.find(c=>c.id===activeId)||clips[0]||null;
 const current=activeClip||null;
 const [editor,setEditor]=useState({...DEFAULT_CLIP});
 useEffect(()=>save('vidigen_learning_history',history),[history]);
 useEffect(()=>save('vidigen_projects',projects),[projects]);
 useEffect(()=>save('vidigen_timeline_v12',clips),[clips]);
 useEffect(()=>()=>{if(generationSource?.preview?.startsWith('blob:')) URL.revokeObjectURL(generationSource.preview)},[generationSource?.preview]);
 useEffect(()=>save('vidigen_active_clip',activeId),[activeId]);
 useEffect(()=>save('vidigen_captions',captions),[captions]);
  useEffect(()=>{localStorage.setItem('vidigen_gateway',gateway);sessionStorage.setItem('vidigen_gateway_token',token);localStorage.setItem('vidigen_learning',memoryOn?'on':'off')},[gateway,token,memoryOn]);
 useEffect(()=>{
   // Registered once, for the app's entire lifetime — NOT inside CustomerAuthScreen, which
   // unmounts the moment a user signs in (it's only rendered while `!token`). The bug that
   // wiring had: the auth-state listener died at exactly the moment session refresh starts
   // mattering. This one keeps running regardless of what's currently rendered, so
   // TOKEN_REFRESHED events keep `token` current automatically, and a real SIGNED_OUT (or
   // an expired refresh token that can't renew) correctly drops back to the sign-in screen.
   if(!supabaseConfigured) return;
   _onUnauthorizedHandler=()=>{setToken('');setShowAuth(true);setStatus('Your session expired — please sign in again.')};
   const {data:sub}=supabase.auth.onAuthStateChange((_event,session)=>{
     if(_event==='PASSWORD_RECOVERY'){ setShowPasswordReset(true); setShowAuth(false); }
     if(session?.access_token) setToken(session.access_token);
     else if(_event!=='PASSWORD_RECOVERY') setToken('');
   });
   supabase.auth.getSession().then(({data})=>{if(data?.session?.access_token) setToken(data.session.access_token)});
   return ()=>{sub.subscription.unsubscribe();_onUnauthorizedHandler=null};
 },[]);
 useEffect(()=>{if(activeClip){setEditor({...DEFAULT_CLIP,...activeClip});setPreviewTime(0)}},[activeId]); useEffect(()=>{const onKeyDown=e=>{const tag=e.target?.tagName?.toLowerCase();const editing=tag==='input'||tag==='textarea'||tag==='select';const mod=e.ctrlKey||e.metaKey;if(mod&&e.key==='Enter'){e.preventDefault();if(!generating)generate();return}if(mod&&!editing&&e.key.toLowerCase()==='z'){e.preventDefault();e.shiftKey?redo():undo();return}if(mod&&!editing&&e.key.toLowerCase()==='y'){e.preventDefault();redo();return}if(e.key==='/'&&!editing){e.preventDefault();document.querySelector('.prompt')?.focus();}};window.addEventListener('keydown',onKeyDown);return()=>window.removeEventListener('keydown',onKeyDown)},[generating,undoStack.length,redoStack.length]);

 useEffect(()=>{let alive=true;(async()=>{try{
   // Check the verified public FastAPI route on the production Cloud Run gateway.
   // /docs is intentionally public, already verified by CI, and avoids relying on the
   // Cloud Run /healthz path that has returned a front-door 404 in some environments.
   let h=await fetch(`https://vidigen-gateway-xvpegaghzq-uc.a.run.app/docs?probe=${Date.now()}`,{cache:'no-store'});
   if(!alive)return;
   setOnline(h.ok);
   // Provider configuration is protected, so only request it once a real Supabase
   // access token exists. A missing provider response must not make the whole app appear offline.
   if(token){
     try{
       const p=await gatewayFetch(gateway,'/api/providers',{},token);
       if(alive)setProviderInfo(await p.json());
     }catch{
       if(alive)setProviderInfo(null);
     }
   }else{
     setProviderInfo(null);
   }
 }catch{
   if(alive){setOnline(false);setProviderInfo(null)}
 }
})();return()=>{alive=false}},[gateway,token]);
 useEffect(()=>{if(nav!=='Billing'||!online)return; let alive=true; (async()=>{try{setBilling(await loadBillingData(gateway,token))}catch(e){if(alive)setStatus(e.message)}})(); return()=>{alive=false}},[nav,online,gateway,token]);
 useEffect(()=>{if(!token||!online)return;let alive=true;(async()=>{try{const r=await gatewayFetch(gateway,'/api/brain/profile',{},token);if(alive)setBrainProfile(await r.json())}catch{if(alive)setBrainProfile({preferred_tags:[],successful_prompts:[],feedback_count:0,learned_outputs:0})}})();return()=>{alive=false}},[token,online,gateway,history.length]);
 function snapshot(){setUndoStack(s=>[...s,clips].slice(-30));setRedoStack([])}
 function replaceClips(next){snapshot();setClips(next)}
 function patchClip(p){if(!activeClip)return;const next={...activeClip,...p};setEditor(e=>({...e,...p}));replaceClips(clips.map(c=>c.id===activeClip.id?next:c));}
 function patchClipById(id,p){setClips(cur=>cur.map(c=>c.id===id?{...c,...p}:c))}
 function generationSourceTypeForMode(){
   if(mode==='Image → Video') return 'image';
   if(mode==='Video → Video') return 'video';
   return null;
 }
 function chooseGenerationSource(file){
   if(!file) return;
   const required=generationSourceTypeForMode();
   const actual=file.type.startsWith('image/')?'image':file.type.startsWith('video/')?'video':null;
   if(!required){setGenerationSource(null);setStatus('This creation mode does not need source media.');return}
   if(!actual){setStatus('Choose a supported image or video file.');return}
   if(actual!==required){setStatus('This mode needs a '+required+' file. Choose a '+required+' source.');return}
   const maxBytes=250*1024*1024;
   if(file.size>maxBytes){setStatus('Source media is larger than 250 MB. Choose a smaller file.');return}
   if(generationSource?.preview?.startsWith('blob:')) URL.revokeObjectURL(generationSource.preview);
   setGenerationSource({file,type:actual,name:file.name,preview:URL.createObjectURL(file)});
   setStatus(file.name+' ready as the '+required+' source.');
 }
 function clearGenerationSource(){
   if(generationSource?.preview?.startsWith('blob:')) URL.revokeObjectURL(generationSource.preview);
   setGenerationSource(null);
 }
 useEffect(()=>{
   const required=generationSourceTypeForMode();
   if(generationSource && required!==generationSource.type) clearGenerationSource();
 },[mode]);
 async function enhancePhoto(){
   if(!activeClip){setStatus('Select a photo first.');return}
   setBgRemoving(true)
   try{
     const blob=await fetch(activeClip.src).then(r=>r.blob())
     if(!blob.type.startsWith('image/'))throw new Error('Photo enhancement is for images only.')
     setStatus('Uploading photo…')
     const presignRes=await gatewayFetch(gateway,'/api/r2-presign',{method:'POST',body:JSON.stringify({object_key:`enhance-src/${activeClip.id}-${Date.now()}${blob.type.includes('png')?'.png':'.jpg'}`,content_type:blob.type})},token)
     if(!presignRes.ok){const e=await presignRes.json().catch(()=>({}));throw new Error(e.detail||'R2 upload setup failed.')}
     const pd=await presignRes.json()
     if(!pd.cdn_url)throw new Error('R2 public URL is not configured.')
     await fetch(pd.upload_url,{method:'PUT',body:blob,headers:{'Content-Type':blob.type}})
     setStatus('Enhancing photo…')
     const r=await gatewayFetch(gateway,'/api/photo-enhance',{method:'POST',body:JSON.stringify({media_url:pd.cdn_url})},token)
     const d=await r.json()
     if(!d.output_url)throw new Error('No enhanced photo returned.')
     const c={id:`enhanced-${Date.now()}`,title:`${activeClip.title||'Photo'} (enhanced)`,kind:'Imported media',src:d.output_url,track:'Video',duration:activeClip.duration||5,...DEFAULT_CLIP}
     replaceClips([...clips,c]);setActiveId(c.id);setStatus('Photo enhanced — added as a new clip.')
   }catch(e){setStatus(`Photo enhancement failed: ${e.message}`)}finally{setBgRemoving(false)}
 }
 async function removeBackground(){
   if(!activeClip){setStatus('Select a clip first.');return}
   setBgRemoving(true)
   try{
     setStatus('Fetching clip data…')
     const blob=await fetch(activeClip.src).then(r=>r.blob())
     setStatus('Uploading to Cloudflare R2…')
     const presignRes=await gatewayFetch(gateway,'/api/r2-presign',{method:'POST',body:JSON.stringify({object_key:`bg-remove/${activeClip.id}-${Date.now()}.mp4`,content_type:blob.type||'video/mp4'})},token)
     if(!presignRes.ok){const err=await presignRes.json().catch(()=>({}));throw new Error(err.detail||'R2 is not configured on the gateway yet.')}
     const {upload_url,cdn_url}=await presignRes.json()
     if(!cdn_url){throw new Error('R2 upload succeeded but no public CDN URL is configured (set R2_PUBLIC_BASE_URL on the gateway).')}
     await fetch(upload_url,{method:'PUT',body:blob,headers:{'Content-Type':blob.type||'video/mp4'}})
     setStatus('Removing background — this can take a couple minutes for video…')
     const kind=isImageMedia(activeClip)?'image':'video'
     const bgRes=await gatewayFetch(gateway,'/api/remove-background',{method:'POST',body:JSON.stringify({media_url:cdn_url,kind})},token)
     if(!bgRes.ok){const err=await bgRes.json().catch(()=>({}));throw new Error(err.detail||'Background removal failed.')}
     const result=await bgRes.json()
     if(!result.output_url){setStatus('Background removal is still processing after the timeout — the source job may still finish on Replicate, but this app has no way to check back in on it yet.');return}
     const c={id:`bgremoved-${Date.now()}`,title:`${activeClip.title||'Clip'} (no bg)`,kind:'Imported media',src:result.output_url,track:'Video',duration:activeClip.duration||5,...DEFAULT_CLIP}
     replaceClips([...clips,c]);setActiveId(c.id)
     setStatus('Background removed — added as a new clip.')
   }catch(e){
     setStatus(`Background removal failed: ${e.message}`)
   }finally{
     setBgRemoving(false)
   }
 }
 async function autoReframe(){
   if(!activeClip){setStatus('Select a clip first.');return}
   const [aw,ah]=ratio.split(':').map(Number)
   if(!aw||!ah){setStatus('Pick an aspect ratio first.');return}
   setReframing(true)
   try{
     setStatus('Uploading clip for reframing…')
     const blob=await fetch(activeClip.src).then(r=>r.blob())
     const presignRes=await gatewayFetch(gateway,'/api/r2-presign',{method:'POST',body:JSON.stringify({object_key:`reframe-src/${activeClip.id}-${Date.now()}.mp4`,content_type:blob.type||'video/mp4'})},token)
     if(!presignRes.ok){const err=await presignRes.json().catch(()=>({}));throw new Error(err.detail||'R2 is not configured on the gateway yet.')}
     const {upload_url,cdn_url}=await presignRes.json()
     if(!cdn_url){throw new Error('R2 upload succeeded but no public CDN URL is configured (set R2_PUBLIC_BASE_URL on the gateway).')}
     await fetch(upload_url,{method:'PUT',body:blob,headers:{'Content-Type':blob.type||'video/mp4'}})
     setStatus('Detecting subject and cropping…')
     const reframeRes=await gatewayFetch(gateway,'/api/auto-reframe',{method:'POST',body:JSON.stringify({media_url:cdn_url,target_aspect_w:aw,target_aspect_h:ah})},token)
     if(!reframeRes.ok){const err=await reframeRes.json().catch(()=>({}));throw new Error(err.detail||'Auto-reframe failed.')}
     const result=await reframeRes.json()
     const c={id:`reframed-${Date.now()}`,title:`${activeClip.title||'Clip'} (${ratio})`,kind:'Imported media',src:result.output_url,track:'Video',duration:activeClip.duration||5,...DEFAULT_CLIP}
     replaceClips([...clips,c]);setActiveId(c.id)
     setStatus(result.subject_detected?`Reframed to ${ratio} around detected subject.`:`Reframed to ${ratio} (no subject detected — centered crop used).`)
   }catch(e){
     setStatus(`Auto-reframe failed: ${e.message}`)
   }finally{
     setReframing(false)
   }
 }
 function deleteClip(){if(!activeClip)return;replaceClips(clips.filter(c=>c.id!==activeClip.id));setActiveId(null);setStatus('Clip deleted.')}
 function duplicateClip(){if(!activeClip)return;const c={...activeClip,id:`clip-${Date.now()}`,title:`${activeClip.title||'Clip'} copy`};replaceClips([...clips,c]);setActiveId(c.id);setStatus('Clip duplicated.')}
 function moveClip(dir){if(!activeClip)return;const i=clips.findIndex(c=>c.id===activeClip.id),j=i+dir;if(j<0||j>=clips.length)return;const a=[...clips];[a[i],a[j]]=[a[j],a[i]];replaceClips(a)}
 function splitClip(){if(!activeClip)return;const cut=Math.max(activeClip.trimStart+0.1,Math.min(activeClip.trimEnd-0.1,(activeClip.trimStart+activeClip.trimEnd)/2));const a={...activeClip,id:`clip-${Date.now()}`,trimEnd:cut,title:`${activeClip.title||'Clip'} A`};const b={...activeClip,id:`clip-${Date.now()+1}`,trimStart:cut,title:`${activeClip.title||'Clip'} B`};const i=clips.findIndex(c=>c.id===activeClip.id);const next=[...clips.slice(0,i),a,b,...clips.slice(i+1)];replaceClips(next);setActiveId(a.id);setStatus('Clip split into two editable segments.')}
 function addKeyframe(){if(!activeClip)return;const t=Math.max(0,Math.min(activeClip.trimEnd-activeClip.trimStart,Number(video.current?.currentTime||0)));const k={time:Number(t.toFixed(2)),x:50,y:50,scale:editor.scale,opacity:editor.opacity,rotation:editor.rotation};patchClip({keyframes:[...(activeClip.keyframes||[]),k].sort((a,b)=>a.time-b.time)});setStatus(`Keyframe added at ${t.toFixed(2)}s.`)}
 function clearKeyframes(){if(activeClip)patchClip({keyframes:[]})}
 function undo(){if(!undoStack.length)return;const prev=undoStack[undoStack.length-1];setRedoStack(r=>[...r,clips].slice(-30));setUndoStack(s=>s.slice(0,-1));setClips(prev)}
 function redo(){if(!redoStack.length)return;const next=redoStack[redoStack.length-1];setUndoStack(s=>[...s,clips].slice(-30));setRedoStack(r=>r.slice(0,-1));setClips(next)}
 async function analyze(){setStatus('Analyzing creative brief…');try{const r=await gatewayFetch(gateway,'/api/analyze',{method:'POST',body:JSON.stringify({prompt})},token);setAnalysis(await r.json());setStatus('Brief analyzed.')}catch{try{if(!geminiConfigured(gateway))throw 0;setAnalysis(await analyzeWithGemini(prompt,gateway,token));setStatus('Gemini analysis ready.')}catch{setAnalysis(normalizeRequest(prompt));setStatus('Local analysis ready.')}}}
 async function improvePrompt(){setStatus('Optimizing creative direction…');try{if(geminiConfigured(gateway)){setPrompt(await improvePromptWithGemini(prompt,mode,gateway,token));}else setPrompt(buildLocalPrompt(prompt,profile,mode));setStatus('Creative brief optimized.')}catch(e){setPrompt(buildLocalPrompt(prompt,profile,mode));setStatus(`Local optimization used: ${e.message}`)}}
 function makeScenes(activeProfile=brainProfile){const total=Math.max(1,parseInt(duration)||5);const count=total>=30?5:total>=15?3:Math.max(1,Math.ceil(total/8));const each=Math.max(2,Math.round((total/count)*10)/10);let learned=memoryOn?buildLocalPrompt(prompt,profile,mode):prompt;if(memoryOn&&(activeProfile?.preferred_tags||[]).length){learned+=` Apply the user's learned creative preferences: ${activeProfile.preferred_tags.join(', ')}.`;}const useAutoCamera=cameraMove===CAMERA_MOVES[0];return Array.from({length:count},(_,i)=>{const motion=useAutoCamera?(i===0?'establishing movement':i===count-1?'controlled closing push-in':'deliberate cinematic movement'):cameraMove;return{id:`scene-${Date.now()}-${i}`,duration:each,prompt:`${learned}. Shot ${i+1} of ${count}; camera direction: ${motion}. Preserve subject identity, lighting, wardrobe, location and visual continuity.`,motion}})}
 async function generate(){
   if(!token){setShowAuth(true);setStatus('Sign in or create a Vidigen account to generate.');return}
   const requiredSourceType=generationSourceTypeForMode();
   if(requiredSourceType==='image' && generationSource?.type && generationSource.type!=='image'){setStatus('Choose an image source for Image → Video.');return}
   if(requiredSourceType==='video' && generationSource?.type && generationSource.type!=='video'){setStatus('Choose a video source for Video → Video.');return}
   if(!online){setStatus('Gateway offline — connect a generation provider first.');return}
   const parsed=normalizeRequest(prompt);if(parsed.riskFlags.length){setStatus('Blocked: '+parsed.riskFlags.join(', '));return}
   let timelineSource=null;
   let sourceType=null;
   if(requiredSourceType){
     if(generationSource?.file){
       timelineSource=await uploadBlobForRender(generationSource.file,'generation-src/'+Date.now()+'-'+Math.random().toString(36).slice(2)+(generationSource.type==='image'?'.jpg':'.mp4'),generationSource.file.type||'application/octet-stream');
       sourceType=generationSource.type;
     }else if(current?.src){
       if(!isImageMedia(current) && requiredSourceType==='image'){setStatus('Select an image or upload one for Image → Video.');return}
       if(isImageMedia(current) && requiredSourceType==='video'){setStatus('Select a video or upload one for Video → Video.');return}
       timelineSource=await prepareGenerationSource(current.src,requiredSourceType);
       sourceType=requiredSourceType;
     }else{
       setStatus('Upload or select a '+requiredSourceType+' source before generating.');return
     }
   }
   setGenerating(true);setProgress(0);let activeProfile=brainProfile;
   try{
     if(token&&online){const rp=await gatewayFetch(gateway,'/api/brain/profile',{},token);activeProfile=await rp.json();setBrainProfile(activeProfile)}
   }catch{}
   const scenes=makeScenes(activeProfile);
   try{
     const out=[];
     for(let i=0;i<scenes.length;i++){
       setStatus('AI Director • generating shot '+(i+1)+'/'+scenes.length);
       const s=scenes[i];
       const r=await generateScene(gateway,token,{prompt:s.prompt,mode,ratio,duration:s.duration+'s',scene:s,sourceUrl:timelineSource,sourceType,model,tags:parsed.tags},st=>setProgress(Math.round(((i+(st.status==='running'?0.5:1))/scenes.length)*100)));
       out.push({...s,...DEFAULT_CLIP,videoUrl:r.url,src:r.url,jobId:r.jobId,track:'Video',mediaType:mode==='Text → Image'?'image':'video',kind:mode==='Text → Image'?'AI image':'AI video',title:'Shot '+(i+1)})
     }
     replaceClips([...clips,...out]);setActiveId(out[0].id);
     const rec={id:Date.now(),prompt,mode,model,jobId:out[0].jobId,timestamp:new Date().toISOString(),rating:0,tags:parsed.tags,success:true};
     if(validateMemoryRecord(rec))setHistory(h=>[rec,...h].slice(0,500));
     setProjects(p=>[{id:Date.now(),title:prompt.slice(0,48),mode,date:new Date().toLocaleDateString(),scenes:out.length,clips:out.map(x=>({...x}))},...p].slice(0,50));
     setStatus('Complete • '+out.length+' shot'+(out.length>1?'s':'')+' added to timeline.');setProgress(100)
   }catch(e){setStatus(e.message)}
   finally{setGenerating(false)}
 }
 async function runAICommand(){if(!aiCommand.trim())return;setCommandBusy(true);setStatus('AI Editor is translating your instruction…');try{const r=await gatewayFetch(gateway,'/api/edit-plan',{method:'POST',body:JSON.stringify({command:aiCommand,clips:clips.map(c=>({id:c.id,title:c.title,duration:c.duration,track:c.track,trimStart:c.trimStart,trimEnd:c.trimEnd})),ratio})},token);const plan=await r.json();if(plan.operations?.length){applyOperations(plan.operations);setStatus(`${plan.engine==='groq'?'Groq':'Keyword'} editor applied ${plan.operations.length} timeline operation${plan.operations.length>1?'s':''}.`)}else setStatus('No safe timeline change was identified.');}catch{const q=aiCommand.toLowerCase();if(q.includes('delete')&&activeClip){deleteClip();setStatus('AI Editor deleted the selected clip.')}else if(q.includes('duplicate')&&activeClip){duplicateClip();setStatus('AI Editor duplicated the selected clip.')}else if(q.includes('split')&&activeClip){splitClip()}else setStatus('AI Editor needs the gateway edit planner for this instruction.')}finally{setCommandBusy(false);setAiCommand('')}}
 function applyOperations(ops){let next=[...clips];for(const op of ops){const i=next.findIndex(c=>c.id===op.clipId);if(op.type==='delete'&&i>=0)next.splice(i,1);else if(op.type==='duplicate'&&i>=0)next.splice(i+1,0,{...next[i],id:`clip-${Date.now()}-${i}`,title:`${next[i].title||'Clip'} copy`});else if(op.type==='trim'&&i>=0)next[i]={...next[i],trimStart:Number(op.trimStart??next[i].trimStart),trimEnd:Number(op.trimEnd??next[i].trimEnd)};else if(op.type==='speed'&&i>=0)next[i]={...next[i],speed:Number(op.value)};else if(op.type==='volume'&&i>=0)next[i]={...next[i],volume:Number(op.value)};else if(op.type==='split'&&i>=0){const c=next[i],cut=(Number(c.trimStart||0)+Number(c.trimEnd??c.duration??5))/2;next.splice(i,1,{...c,id:`clip-${Date.now()}-${i}a`,trimEnd:cut,title:`${c.title||'Clip'} A`},{...c,id:`clip-${Date.now()}-${i}b`,trimStart:cut,title:`${c.title||'Clip'} B`})}else if(op.type==='keyframe'&&i>=0)next[i]={...next[i],keyframes:[...(next[i].keyframes||[]),op.keyframe]}}replaceClips(next)}
 async function transcribe(){if(!audio?.blob&&!audio?.file&&!activeClip?.src){setStatus('Add audio or select a video clip first.');return}setCaptioning(true);setStatus('Transcribing audio…');try{let f=audio?.file||audio?.blob;let name=audio?.name||'audio.webm';if(!f&&activeClip?.src){const resp=await fetch(activeClip.src);if(!resp.ok)throw new Error('Could not read the selected video for captions.');f=await resp.blob();name=activeClip.title||'selected-video.mp4'}const fd=new FormData();fd.append('file',f,name);const data=await (await gatewayFetch(gateway,'/api/captions',{method:'POST',body:fd},token)).json();setCaptions(data.segments||[]);setStatus(String((data.segments||[]).length)+' caption segments created.')}catch(e){setStatus(e.message)}finally{setCaptioning(false)}}
 function importAudio(file){if(!file)return;const url=URL.createObjectURL(file);setAudio({name:file.name,url,file});setStatus(`${file.name} added to audio track.`)}
 function startRecording(){if(!navigator.mediaDevices?.getUserMedia){setStatus('Microphone recording is unavailable on this device/browser.');return}navigator.mediaDevices.getUserMedia({audio:true}).then(stream=>{const r=new MediaRecorder(stream);chunks.current=[];r.ondataavailable=e=>e.data.size&&chunks.current.push(e.data);r.onstop=()=>{const blob=new Blob(chunks.current,{type:'audio/webm'});setAudio({name:'Voiceover recording.webm',url:URL.createObjectURL(blob),blob});stream.getTracks().forEach(t=>t.stop());setStatus('Voiceover recording added.')};recorder.current=r;r.start();setRecording(true);setStatus('Recording voiceover…')}).catch(e=>setStatus(`Microphone error: ${e.message}`))}
 function stopRecording(){recorder.current?.stop();setRecording(false)}
 function downloadSrt(){if(!captions.length){setStatus('Create captions first.');return}const fmt=t=>{const ms=Math.round(t*1000),h=Math.floor(ms/3600000),m=Math.floor(ms%3600000/60000),s=Math.floor(ms%60000/1000),z=ms%1000;return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')},${String(z).padStart(3,'0')}`};const text=captions.map((c,i)=>`${i+1}\n${fmt(c.start)} --> ${fmt(c.end)}\n${c.text}\n`).join('\n');const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([text],{type:'text/plain'}));a.download='vidigen-captions.srt';a.click()}
 async function rate(n){if(!history.length){setStatus('Generate a result before rating it.');return}const latest=history[0];setHistory(h=>h.map((x,i)=>i===0?{...x,rating:n}:x));try{if(latest.jobId&&token){await gatewayFetch(gateway,'/api/feedback',{method:'POST',body:JSON.stringify({job_id:latest.jobId,rating:n,accepted:n>=4,signals:{tags:latest.tags||[],mode:latest.mode,model:latest.model}})},token);const rp=await gatewayFetch(gateway,'/api/brain/profile',{},token);setBrainProfile(await rp.json());setStatus(`Feedback saved • Brain updated (${n}/5).`);return}}catch{}setStatus(`Feedback saved locally: ${n}/5.`)}
 function addOverlay(){if(!activeClip||!overlay.text.trim())return;patchClip({overlay:{...overlay}});setStatus('Text overlay attached to selected clip.')}
 async function uploadBlobForRender(blob,objectKey,mime){
   const presignRes=await gatewayFetch(gateway,'/api/r2-presign',{method:'POST',body:JSON.stringify({object_key:objectKey,content_type:mime||'application/octet-stream'})},token)
   const signed=await presignRes.json(); if(!signed.upload_url||!signed.cdn_url) throw new Error('Durable R2 storage is required for browser rendering.')
   const put=await fetch(signed.upload_url,{method:'PUT',body:blob,headers:{'Content-Type':mime||'application/octet-stream'}}); if(!put.ok) throw new Error('R2 upload failed.')
   return signed.cdn_url
 }
 async function prepareGenerationSource(sourceUrl){
   if(!sourceUrl) return null;
   try{
     const parsed=new URL(sourceUrl,window.location.href);
     if(parsed.protocol==='http:'||parsed.protocol==='https:') return sourceUrl;
   }catch{}
   const blob=await fetch(sourceUrl).then(r=>{if(!r.ok)throw new Error('Could not read the selected source media.');return r.blob()});
   if(!blob.size) throw new Error('Selected source media is empty.');
   const mime=blob.type||'application/octet-stream';
   const ext=mime.includes('png')?'png':mime.includes('webp')?'webp':mime.includes('jpeg')||mime.includes('jpg')?'jpg':mime.includes('mp4')?'mp4':mime.includes('webm')?'webm':'bin';
   setStatus('Uploading source media…');
   return uploadBlobForRender(blob,`generation-src/${Date.now()}-${Math.random().toString(36).slice(2)}.${ext}`,mime);
 }
 async function exportProject(){
   setExportBusy(true)
   try{
     if(nativeExportAvailable() && !clips.some(isImageMedia)){
       setStatus('Exporting locally on-device…')
       const prepared=[]
       for(const c of clips.filter(c=>c.track!=='Audio')){
         const uri=c.nativeUri||c.src
         if(!uri?.startsWith('blob:')) prepared.push({uri,trimStartMs:Math.round((c.trimStart||0)*1000),trimEndMs:c.trimEnd?Math.round(c.trimEnd*1000):undefined})
         else throw new Error('Imported local clip is not available to the native exporter. Wait for device storage preparation, then try again.')
       }
       const backgroundAudioUri=audio?.url||undefined
       if(backgroundAudioUri?.startsWith('blob:')) throw new Error('Browser-recorded audio must be stored in device storage before native export.')
       const result=await exportProjectNative({clips:prepared,backgroundAudioUri},p=>setStatus(`Exporting… ${Math.round(p)}%`))
       setStatus(`Export complete • ${Math.round((result.durationMs||0)/1000)}s master created on device.`); setShowExport(false); return
     }
     setStatus('Preparing browser render…')
     const renderClips=[]
     for(const c of clips.filter(c=>c.track!=='Audio')){
       let uri=c.src
       if(uri?.startsWith('blob:')){const blob=await fetch(uri).then(r=>r.blob());uri=await uploadBlobForRender(blob,`renders/source-${c.id}-${Date.now()}`,blob.type||'video/mp4')}
       if(!uri?.startsWith('http')) throw new Error(`Clip ${c.title||c.id} is not renderable until it has a durable HTTP media URL.`)
       renderClips.push({uri,mediaType:isImageMedia(c)?'image':'video',trimStartMs:Math.round((c.trimStart||0)*1000),trimEndMs:c.trimEnd?Math.round(c.trimEnd*1000):undefined})
     }
     let backgroundAudioUri=audio?.url||undefined
     if(backgroundAudioUri?.startsWith('blob:')){const blob=await fetch(backgroundAudioUri).then(r=>r.blob());backgroundAudioUri=await uploadBlobForRender(blob,`renders/audio-${Date.now()}`,blob.type||'audio/webm')}
     const data=await (await gatewayFetch(gateway,'/api/render',{method:'POST',body:JSON.stringify({ratio,clips:renderClips,backgroundAudioUri:backgroundAudioUri||null})},token)).json()
     if(!data.output_url) throw new Error('Render completed without a durable output URL.')
     setStatus(`Export complete • ${Math.round(data.duration)}s master ready.`); setShowExport(false)
   }catch(e){setStatus(`Export failed: ${e.message}`)}finally{setExportBusy(false)}
 }

 const filter=`brightness(${editor.brightness}%) contrast(${editor.contrast}%) saturate(${editor.saturation}%) blur(${editor.blur}px)`;
 const isImageMedia=(c)=>!!c&&(c.mediaType==='image'||String(c.kind||'').toLowerCase().includes('image')||/\.(png|jpe?g|webp|gif|avif)$/i.test(String(c.title||'')));
 const requestedCapability=mode==='Text → Image'?'image':mode==='Image → Video'?'image-to-video':mode==='Video → Video'?'video-to-video':'video';
 const availableModels=[['auto','Auto Router'],...((providerInfo?.providers||[]).filter(p=>p.configured&&p.key!=='local'&&(p.capabilities||[]).some(c=>c===requestedCapability||(requestedCapability!=='image'&&c==='video'))).map(p=>[p.key,String(p.key).replace(/[-_]+/g,' ').replace(/\b\w/g,m=>m.toUpperCase())]))];
 return <div className="app">
  <header className="topbar"><div className="brand"><div className="brandMark">V</div><span>Vidigen</span><b>V12</b></div><div className="projectTitle">AI Production Studio<small>{clips.length} clips • {captions.length} captions • {profile.successCount} learned preferences</small></div><div className="topActions"><span className={`enginePill ${online?'online':''}`}><i/> {online?'Gateway online':'Offline'}</span><button className="ghost" title="Undo (Ctrl/⌘ + Z)" onClick={undo} disabled={!undoStack.length}>Undo</button><button className="ghost" title="Redo (Ctrl/⌘ + Shift + Z)" onClick={redo} disabled={!redoStack.length}>Redo</button><button className="ghost" title="Open Creative Brain" onClick={()=>setShowBrain(true)}>Brain</button><button className="share" onClick={()=>setStatus('Project link sharing is available when persistence/auth is configured.')}>Share</button><button className="export" onClick={()=>setShowExport(true)}>Export</button>{!token&&<button className="ghost" onClick={()=>setShowAuth(true)}>Sign in</button>}<div className="avatar">JA</div></div></header>
  <div className="editor">
   <nav className="rail">
  <div className="railGroup">
    {NAV.map(([n,icon])=><button key={n} className={`railItem ${nav===n?'active':''}`} onClick={()=>setNav(n)}><strong>{icon}</strong><span>{n==='Media'?'Assets':n==='Effects'?'Edit':n}</span></button>)}
  </div>
  <div className="railSpacer"/>
  <div className="railUtilities">
    <button className={`railItem ${nav==='Projects'?'active':''}`} onClick={()=>setNav('Projects')}><strong>□</strong><span>Projects</span></button>
    <button className={`railItem ${nav==='Billing'?'active':''}`} onClick={()=>setNav('Billing')}><strong>¤</strong><span>Credits</span></button>
    <button className="railItem" onClick={()=>setShowSettings(true)}><strong>⚙</strong><span>Settings</span></button>
  </div>
</nav>
   <main className="center"><div className="previewHeader"><div><b>{mode}</b><span>{ratio} • {duration}</span></div><div className="previewActions"><button onClick={()=>{if(video.current)video.current.currentTime=Math.max(0,video.current.currentTime-1)}}>−1s</button><button onClick={()=>video.current?.play()}>Play</button><button onClick={()=>video.current?.pause()}>Pause</button><button onClick={()=>{if(video.current)video.current.currentTime+=1}}>+1s</button></div></div>
    <div className="stage">
      {current?.src ? <div className={"videoFrame ratio-"+ratio.replace(":","-").replace(".","-")} style={{filter}}>
        {isImageMedia(current) ? <img src={current.src} alt={current.title||"Generated image"} className="mediaPreview"/> : <video ref={video} src={current.src} controls={false} playsInline onTimeUpdate={e=>setPreviewTime(e.currentTarget.currentTime)}/>}
        {!isImageMedia(current) && <button className="bigPlay" aria-label="Play preview" onClick={()=>video.current?.paused?video.current?.play():video.current?.pause()}>▶</button>}
        {(()=>{const seg=captions.find(x=>previewTime>=Number(x.start||0)&&previewTime<=Number(x.end??Infinity))||captions[0];return seg?<div className={"captionOverlay "+captionStyle.toLowerCase()}>{seg.text}</div>:null})()}
        {activeClip?.overlay?.text&&<div className="customOverlay" style={{left:`${activeClip.overlay.x}%`,top:`${activeClip.overlay.y}%`,fontSize:`${activeClip.overlay.size}px`,fontWeight:activeClip.overlay.bold?800:500}}>{activeClip.overlay.text}</div>}
        <div className="stageInfo"><span>{activeClip?.title||"Preview"}</span><span>{isImageMedia(current)?"Image":ratio+" • "+duration}</span></div>
      </div> : <div className="emptyCanvas"><div className="emptyCanvasContent"><span className="eyebrow">✦ AI Director ready</span><h1>Turn an idea into a scene.</h1><p>Describe the subject, mood, movement and outcome. Vidigen will shape the brief, generate the shot and place it on your timeline.</p><div className="miniHint">Start in the Director panel → Generate</div></div></div>}
    </div>
    <div className="transport"><button onClick={()=>{if(video.current)video.current.currentTime=0}}>⏮</button><button onClick={()=>video.current?.paused?video.current?.play():video.current?.pause()}>▶/Ⅱ</button><button onClick={()=>{if(video.current)video.current.currentTime=video.current.duration||0}}>⏭</button><div className="scrub" onClick={e=>{if(!video.current?.duration)return;const t=(e.nativeEvent.offsetX/e.currentTarget.clientWidth)*video.current.duration;video.current.currentTime=t;setPreviewTime(t)}}><div style={{width:`${video.current?.duration?((previewTime/video.current.duration)*100):0}%`}}/></div><span>{status}{generating?` • ${progress}%`:''}</span></div>
   </main>
   <aside className="inspector">
    {nav==='Create'&&<>
  <div className="inspectorTop"><div><b>Create</b><small className="modalSub">Describe what you want. Vidigen handles the production steps.</small></div><span className="tinyBadge">1 • CREATE</span></div>
  <div className="createSteps"><span className="active"><b>1</b> Idea</span><span><b>2</b> Style</span><span><b>3</b> Generate</span></div>
  <label className="sectionLabel">What are you making?</label>
  <div className="modeGrid">{MODES.map(m=><button key={m} className={\`mode \${mode===m?'active':''}\`} onClick={()=>setMode(m)}>{m}</button>)}</div>
  {generationSourceTypeForMode()&&<div className="sourceCard">
    <div className="sourceHead"><div><b>{generationSourceTypeForMode()==='image'?'Source image':'Source video'}</b><small>Use an uploaded file or a matching timeline asset.</small></div>{generationSource&&<button className="textButton" onClick={clearGenerationSource}>Remove</button>}</div>
    {generationSource
      ? <div className="sourcePreview">{generationSource.type==='image'?<img src={generationSource.preview} alt="" />:<video src={generationSource.preview} muted playsInline controls={false}/>}<div><b>{generationSource.name}</b><small>Ready for {mode}</small></div></div>
      : <div className="sourceDrop"><label className="uploadSourceButton"><input type="file" hidden accept={generationSourceTypeForMode()==='image'?'image/*':'video/*'} onChange={e=>chooseGenerationSource(e.target.files?.[0])}/>{generationSourceTypeForMode()==='image'?'Choose image':'Choose video'}</label><span>or select a matching asset in Media</span></div>}
    </div>}
  <label className="sectionLabel">Your idea</label>
  <textarea className="prompt" value={prompt} onChange={e=>setPrompt(e.target.value)} placeholder="Describe the subject, action, look and result you want…"/>
  <div className="promptMeta"><span>Write naturally — Vidigen turns this into a production brief.</span><kbd>Ctrl/⌘ + Enter</kbd></div>
  <div className="promptChips"><button type="button" onClick={()=>{setPrompt('Create a premium cinematic product ad with a bold opening hook, elegant close-ups, controlled camera motion and a clear final CTA.');setMode('Text → Video')}}>Product ad</button><button type="button" onClick={()=>{setPrompt('Create a fast, energetic vertical social video with a strong 2-second hook, punchy cuts, captions and a memorable ending.');setRatio('9:16')}}>Social short</button><button type="button" onClick={()=>setPrompt('Create a cinematic story with consistent character identity, dramatic lighting, rich composition, deliberate camera movement and a satisfying visual payoff.')}>Cinematic</button></div>
  <div className="promptActions"><button onClick={analyze}>Check idea</button><button onClick={improvePrompt}>Improve prompt</button></div>
  {analysis&&<div className="analysis"><b>{analysis.type||'video'}</b><span>{analysis.summary||'Ready for production.'}</span><small>{(analysis.tags||[]).join(' • ')}</small></div>}
  <details className="advancedSettings">
    <summary><span>Fine-tune generation</span><small>Format, length, camera and model</small></summary>
    <div className="advancedBody">
      <div className="twoFields"><div><label>Format</label><select value={ratio} onChange={e=>setRatio(e.target.value)}>{RATIOS.map(x=><option key={x}>{x}</option>)}</select></div><div><label>Length</label><select value={duration} onChange={e=>setDuration(e.target.value)}>{DURATIONS.map(x=><option key={x}>{x}</option>)}</select></div></div>
      <label>Camera</label><select value={cameraMove} onChange={e=>setCameraMove(e.target.value)}>{CAMERA_MOVES.map(x=><option key={x}>{x}</option>)}</select>
      <label>Engine</label><select value={model} onChange={e=>setModel(e.target.value)}>{availableModels.map(([id,n])=><option key={id} value={id}>{n}</option>)}</select>
      {activeClip&&<button disabled={reframing||!activeClip||isImageMedia(activeClip)} onClick={autoReframe}>{reframing?'Reframing…':isImageMedia(activeClip)?'Auto-reframe is for video':'Auto-reframe selected clip'}</button>}
    </div>
  </details>
  <div className="generationSummary"><div><span>{mode==='AI Avatar'?'Avatar provider required':'Ready to generate'}</span><b>{mode} • {duration} • {ratio}</b></div><span className={mode==='AI Avatar'&&!((providerInfo?.providers||[]).some(p=>p.key==='avatar-gateway'&&p.configured))?'badText':online?'okText':'badText'}>{mode==='AI Avatar'&&!((providerInfo?.providers||[]).some(p=>p.key==='avatar-gateway'&&p.configured))?'● Provider unavailable':online?'● Provider ready':'● Connection needs attention'}</span></div>
  <button className="generate" disabled={generating} onClick={generate}>{generating?`Generating ${progress}%`:'Generate'}<small>{generating?'Vidigen is creating your result — you can keep watching the preview.':'Your result will appear in the canvas and timeline.'}</small></button>
</>}
    {nav==='Media'&&<div className="sectionCard"><b>Media library</b><p>Import your own production footage or images into the timeline.</p><input type="file" accept="video/*,image/*" onChange={e=>{const f=e.target.files?.[0];if(f){const mediaType=f.type.startsWith('image/')?'image':f.type.startsWith('video/')?'video':null;
 if(!mediaType){setStatus('Only image and video files are supported.');return}
 const c={id:\`media-\${Date.now()}\`,title:f.name,kind:'Imported media',src:URL.createObjectURL(f),track:'Video',duration:5,mediaType,...DEFAULT_CLIP};replaceClips([...clips,c]);setActiveId(c.id);copyFileToNativeStorage(f).then(nativeUri=>{if(nativeUri)patchClipById(c.id,{nativeUri})}).catch(()=>{})}}}/>{clips.length?<div className="mediaImported"><b>{clips.length} production asset(s) in this project</b><small>Assets are project-scoped and come only from this project or its AI generation jobs.</small></div>:<div className="emptyState"><b>No media imported yet</b><span>Upload production footage or generate new assets with AI Director.</span></div>}</div>}
    {nav==='Text'&&<>
      <div className="sectionCard">
        <b>Text &amp; motion</b>
        <label>Overlay text</label>
        <input value={overlay.text} onChange={e=>setOverlay({...overlay,text:e.target.value})} placeholder="Add a title, hook or CTA…"/>
        <div className="twoFields">
          <div><label>Size</label><input type="number" min="12" max="120" value={overlay.size} onChange={e=>setOverlay({...overlay,size:+e.target.value})}/></div>
          <div><label>Vertical position</label><input type="number" min="5" max="95" value={overlay.y} onChange={e=>setOverlay({...overlay,y:+e.target.value})}/></div>
        </div>
        <button onClick={addOverlay} disabled={!activeClip||!overlay.text.trim()}>{activeClip?'Add to selected clip':'Select a clip first'}</button>
      </div>
      <div className="sectionCard">
        <b>AI Presenter</b>
        <span className="badText">Presenter generation is not enabled on the production gateway.</span>
        <p>Vidigen will show presenter, voice and avatar controls here once a production avatar provider is connected.</p>
      </div>
    </>}    {nav==='Audio'&&<><div className="sectionCard"><b>Audio studio</b><input ref={audioInput} type="file" accept="audio/*,video/*" onChange={e=>importAudio(e.target.files?.[0])}/><div className="audioButtons"><button onClick={recording?stopRecording:startRecording}>{recording?'Stop recording':'Record voiceover'}</button><button onClick={transcribe} disabled={(!audio&&!activeClip)||captioning}>{captioning?'Transcribing…':'Auto captions'}</button></div>{audio&&<audio controls src={audio.url}/>}</div><div className="sectionCard"><b>Selected clip mix</b>{activeClip&&<><label>Volume {Math.round(editor.volume*100)}%</label><input type="range" min="0" max="2" step=".01" value={editor.volume} onChange={e=>patchClip({volume:+e.target.value})}/><label>Speed {editor.speed}×</label><input type="range" min=".25" max="4" step=".05" value={editor.speed} onChange={e=>patchClip({speed:+e.target.value})}/></>}</div></>}
    {nav==='Effects'&&<>
      <div className="sectionCard aiCommandCard"><b>AI Edit</b><span>Tell Vidigen what to change. Example: “trim the selected clip to 3 seconds” or “make this clip 1.5× faster.”</span><div className="aiCommandRow"><input value={aiCommand} onChange={e=>setAiCommand(e.target.value)} placeholder="Describe an edit…"/><button onClick={runAICommand} disabled={commandBusy||!aiCommand.trim()}>{commandBusy?'Working…':'Apply'}</button></div></div><div className="sectionCard"><b>Professional controls</b><div className="twoFields"><div><label>Trim in</label><input type="number" min="0" step=".1" value={editor.trimStart} onChange={e=>patchClip({trimStart:+e.target.value})}/></div><div><label>Trim out</label><input type="number" min="0" step=".1" value={editor.trimEnd} onChange={e=>patchClip({trimEnd:+e.target.value})}/></div></div><div className="twoFields"><div><label>Brightness</label><input type="range" min="50" max="150" value={editor.brightness} onChange={e=>patchClip({brightness:+e.target.value})}/></div><div><label>Contrast</label><input type="range" min="50" max="150" value={editor.contrast} onChange={e=>patchClip({contrast:+e.target.value})}/></div></div><div className="twoFields"><div><label>Saturation</label><input type="range" min="0" max="200" value={editor.saturation} onChange={e=>patchClip({saturation:+e.target.value})}/></div><div><label>Blur</label><input type="range" min="0" max="12" value={editor.blur} onChange={e=>patchClip({blur:+e.target.value})}/></div></div><label>Scale {editor.scale}%</label><input type="range" min="50" max="150" value={editor.scale} onChange={e=>patchClip({scale:+e.target.value})}/><label>Opacity {editor.opacity}%</label><input type="range" min="0" max="100" value={editor.opacity} onChange={e=>patchClip({opacity:+e.target.value})}/><label>Rotation {editor.rotation}°</label><input type="range" min="-180" max="180" value={editor.rotation} onChange={e=>patchClip({rotation:+e.target.value})}/><div className="audioButtons"><button onClick={splitClip}>Split</button><button onClick={duplicateClip}>Duplicate</button><button onClick={deleteClip}>Delete</button></div><div className="audioButtons"><button disabled={reframing||!activeClip||isImageMedia(activeClip)} onClick={autoReframe}>{reframing?'Reframing…':isImageMedia(activeClip)?'Auto-reframe is for video':'Auto-reframe'} </button><button disabled={bgRemoving||!activeClip} onClick={removeBackground}>{bgRemoving?'Removing background…':'Remove background'}</button></div><div className="audioButtons"><button disabled={bgRemoving||!activeClip||!isImageMedia(activeClip)} onClick={enhancePhoto}>{bgRemoving?'Enhancing…':isImageMedia(activeClip)?'Enhance selected photo':'Select a photo to enhance'}</button></div>{activeClip&&<p className="hint">Background removal uploads this clip to your configured Cloudflare R2 bucket, then runs it through Replicate — needs R2 and REPLICATE_API_TOKEN set on the gateway.</p>}</div><div className="sectionCard"><b>Keyframes</b><p>Animate transform properties directly on the timeline.</p><button onClick={addKeyframe}>Add keyframe at playhead</button><button onClick={clearKeyframes}>Clear keyframes</button>{activeClip?.keyframes?.map((k,i)=><div className="keyframeRow" key={i}><span>{k.time.toFixed(2)}s</span><small>scale {k.scale}% • opacity {k.opacity}% • rot {k.rotation}°</small></div>)}</div></>}
    {nav==='Captions'&&<><div className="sectionCard"><b>Caption studio</b><p>{captions.length?`${captions.length} timed segments ready.`:(activeClip?'Select Auto captions to transcribe this clip.':'Add or select media to create captions.')}</p><button onClick={transcribe} disabled={(!audio&&!activeClip)||captioning}>{captioning?'Transcribing…':audio?'Transcribe audio':'Transcribe selected video'}</button><button onClick={downloadSrt}>Export SRT</button><label>Style</label><select value={captionStyle} onChange={e=>setCaptionStyle(e.target.value)}><option>Bold</option><option>Clean</option><option>Minimal</option></select></div><div className="captionList">{captions.slice(0,40).map((c,i)=><div key={i}><time>{c.start.toFixed(2)}s</time><span>{c.text}</span></div>)}</div></>}
    {nav==='Billing'&&<div className="sectionCard">
      <b>Vidigen Plans &amp; Credits</b>
      <p>Subscriptions use monthly credits so premium video generations remain cost-controlled. Prices and credit budgets are managed server-side.</p>
      {billing?.subscription&&<div className="analysis"><b>{billing.subscription.plan_slug}</b><span>Active until {new Date(billing.subscription.current_period_end).toLocaleDateString()}</span></div>}
      <div className="cards">
        {(billing?.plans||[]).map(p=><div className="card" key={p.slug}>
          <b>{p.name}</b>
          <strong>{p.price_ghs===0?'Free':`GHS ${Number(p.price_ghs).toFixed(0)}/month`}</strong>
          <small>{p.monthly_credits.toLocaleString()} credits • {p.storage_gb} GB{p.watermark?' • watermark':''}{p.commercial_use?' • commercial':''}</small>
          {p.slug==='free'
            ? <button disabled>Current free tier</button>
            : <div className="audioButtons">
                <button disabled={billingBusy} onClick={async()=>{setBillingBusy(true);try{await startBillingCheckout(gateway,token,p.slug,'paystack')}catch(e){setStatus(e.message)}finally{setBillingBusy(false)}}}>{billingBusy?'Opening…':'Pay with Paystack'}</button>
                <button disabled title="Google Pay production is separate and remains disabled until Google approval and a confirmed production processor are configured.">Google Pay (pending)</button>
              </div>
          }
        </div>)}
      </div>
      <div className="hint">Free plan: 5 avatar generations and 5 photo enhancements per day, with 500 MB storage. Video generation is available through purchased credits or a paid plan.</div>
      <div className="analysis"><b>Video credits</b><span>150 credits • GHS 30.00</span><button onClick={async()=>{try{const r=await gatewayFetch(gateway,'/api/billing/checkout/credits',{method:'POST',body:JSON.stringify({credits:150})},token);const d=await r.json();if(!d.authorization_url)throw new Error('No Paystack checkout URL returned.');window.location.href=d.authorization_url}catch(e){setStatus(e.message)}}}>Buy 150 credits</button></div>
      <div className="analysis"><b>Credit balance</b><span>{billing?.wallet?.balance??'—'} credits remaining</span></div>
      <div className="paymentNote"><span>Payments are processed securely through Paystack.</span><small>Google Pay support will appear here when the production processor and approval are confirmed.</small></div>
    </div>}
    {nav==='Projects'&&<div className="sectionCard"><b>Projects</b><p>{projects.length} projects saved in this browser. Generation jobs can also be persisted to your account.</p>{projects.length?projects.map(p=><button className="mediaLine" key={p.id} onClick={()=>{if(Array.isArray(p.clips)&&p.clips[0]&&typeof p.clips[0]==='object'){setClips(p.clips);setActiveId(p.clips[0]?.id||null);setNav('Effects');setStatus(`Loaded project: ${p.title}`)}else setStatus('This older project record does not contain media snapshots. Generate it again to rebuild the timeline.')}}><div className="projectThumb">AI</div><span>{p.title}<small>{p.mode} • {p.scenes} shots • {p.date}</small></span></button>):<div className="emptyState"><b>No saved projects yet</b><span>Generate something and Vidigen will save the project snapshot here.</span></div>}</div>}
   </aside>
  </div>
  <section className="timelinePanel"><div className="timelineTop"><div><b>Timeline</b><span>{clips.length} clips • {Math.round(clips.reduce((a,c)=>a+Math.max(0,Number(c.trimEnd??c.duration??5)-Number(c.trimStart||0)),0)*10)/10}s</span></div><div className="timelineButtons"><button onClick={()=>moveClip(-1)}>←</button><button onClick={()=>moveClip(1)}>→</button><button onClick={splitClip}>Split</button><button onClick={duplicateClip}>Duplicate</button><button onClick={deleteClip}>Delete</button><button onClick={()=>setZoom(z=>Math.min(2,z+.1))}>Zoom +</button><button onClick={()=>setZoom(z=>Math.max(.5,z-.1))}>Zoom −</button></div></div><div className="timelineBody"><div className="labels"><span>VIDEO</span><span>OVERLAY</span><span>TEXT</span><span>AUDIO</span></div><div className="tracks" style={{'--zoom':zoom}}><div className="ruler">00:00　　00:05　　00:10　　00:15　　00:20　　00:25　　00:30</div><div className="row videoRow">{clips.filter(c=>c.track!=='Audio').map((c,i)=><button key={c.id} className={`clip ${activeClip?.id===c.id?'selected':''}`} style={{width:`${Math.max(110,(Number(c.trimEnd??c.duration??5)-Number(c.trimStart||0))*34*zoom)}px`}} onClick={()=>{setActiveId(c.id);setNav('Effects')}}><span>{c.title||`Shot ${i+1}`}</span><small>{Math.max(0,Number(c.trimEnd??c.duration??5)-Number(c.trimStart||0)).toFixed(1)}s</small></button>)}</div><div className="row"><div className="emptyClip">Overlay track • attach text, masks or graphics</div></div><div className="row"><div className="textClip">{captions.length?`CC • ${captions.length} timed segments`:'Text / captions track'}</div></div><div className="row"><div className="audioClip">{audio?`♫ ${audio.name}`:'Audio / music / SFX track'}</div></div><div className="playhead" style={{left:video.current?.duration?`${Math.min(100,(previewTime/video.current.duration)*100)}%`:'0%'}}/></div></div></section>
  <section className="bottom"><div className="bottomHead"><b>Production assets</b><span>{clips.length} asset(s) in this project</span></div>{clips.length?<div className="cards">{clips.slice(0,12).map((c,i)=><button className="card" key={c.id} onClick={()=>{setActiveId(c.id);setNav('Effects')}}>{isImageMedia(c)?<img src={c.src} alt="" className="assetThumb"/>:<video src={c.src} muted preload="metadata"/>}<span><b>{c.title||`Shot ${i+1}`}</b><small>{c.kind||'Production asset'}</small></span></button>)}</div>:<div className="emptyState"><b>Your production assets will appear here</b><span>Generate or import media to begin.</span></div>}</section>
  <div className="feedback"><span>Teach the Brain from the latest result</span><button onClick={()=>rate(5)}>★ Excellent</button><button onClick={()=>rate(3)}>Good</button><button onClick={()=>rate(1)}>Needs work</button><button onClick={()=>setShowBrain(true)}>View Brain</button></div>
  {showBrain&&<div className="modalBack"><div className="modal wideModal"><div className="modalHead"><div><b>Vidigen Creative Brain</b><small className="modalSub">Persistent preference learning and output memory</small></div><button onClick={()=>setShowBrain(false)}>×</button></div><div className="stats"><div><b>{brainProfile.learned_outputs||history.length}</b><span>learned outputs</span></div><div><b>{brainProfile.feedback_count||0}</b><span>feedback signals</span></div><div><b>{(brainProfile.preferred_tags||profile.preferredTags||[]).length}</b><span>style preferences</span></div></div><div className="brainStatus"><span className="statusDot"/><div><b>{online?'Brain connected to server':'Local Brain only'}</b><small>{online?'Completed outputs and feedback can persist to your Vidigen account.':'Sign in and connect the production gateway to enable persistent server learning.'}</small></div></div><label>Learned style signals</label><div className="tags">{(brainProfile.preferred_tags||profile.preferredTags||[]).length?(brainProfile.preferred_tags||profile.preferredTags).map(t=><span key={t}>{t}</span>):<small>No high-confidence preferences yet. Rate completed work to teach the Brain.</small>}</div><label>How learning works</label><p className="hint">Each completed job can be analyzed into reusable creative metadata. Only explicit positive feedback influences preferred style signals. Vidigen does not silently retrain third-party foundation models.</p><button className="danger" onClick={()=>{setHistory([]);setBrainProfile({preferred_tags:[],successful_prompts:[],feedback_count:0,learned_outputs:0});setStatus('Local creative memory cleared. Server Brain data remains account-scoped.')}}>Clear local memory</button></div></div>}
  {showSettings&&<div className="modalBack"><div className="modal wideModal">
    <div className="modalHead"><div><b>Settings</b><small className="modalSub">Keep creator settings simple. Technical controls live under Advanced.</small></div><button onClick={()=>setShowSettings(false)}>×</button></div>
    <div className="settingsTabs settingsTabsSimple">
      <button className={settingsTab==='general'?'active':''} onClick={()=>setSettingsTab('general')}>Workspace</button>
      <button className={settingsTab==='ai'?'active':''} onClick={()=>setSettingsTab('ai')}>AI &amp; Generation</button>
      <button className={settingsTab==='account'?'active':''} onClick={()=>setSettingsTab('account')}>Account</button>
      <button className={settingsTab==='advanced'?'active':''} onClick={()=>setSettingsTab('advanced')}>Advanced</button>
    </div>
    {settingsTab==='general'&&<div className="settingsGrid">
      <div className="settingsSection"><b>Workspace</b><span>Your editor stays focused on the creative workflow. These settings affect this workspace only.</span>
        <div className="settingsInline"><span>Connection</span><strong className={online?'okText':'badText'}>{online?'Online':'Offline'}</strong></div>
        <div className="settingsInline"><span>Interface</span><strong>Creator mode</strong></div>
        <div className="settingsInline"><span>Keyboard</span><strong>⌘/Ctrl shortcuts enabled</strong></div>
      </div>
      <div className="settingsSection"><b>Storage</b><span>{billing?.wallet?'Your account is connected to server-side billing.':'Open Credits to view your plan and storage quota.'}</span>
        <div className="stats"><div><b>{billing?.subscription?.plan_slug||'Free'}</b><span>plan</span></div><div><b>{billing?.plans?.find(p=>p.slug===(billing?.subscription?.plan_slug||'free'))?.storage_gb||0.5} GB</b><span>quota</span></div><div><b>{brainProfile.learned_outputs||0}</b><span>learned outputs</span></div></div>
      </div>
    </div>}
    {settingsTab==='ai'&&<div className="settingsGrid">
      <div className="settingsSection"><b>Generation defaults</b><span>Start with simple controls. Fine-tuning remains available inside the Create panel when you need it.</span>
        <div className="settingsInline"><span>Routing</span><strong>Auto</strong></div>
        <div className="settingsInline"><span>Quality path</span><strong>Server controlled</strong></div>
        <div className="settingsInline"><span>Fallback</span><strong>Automatic</strong></div>
        <div className="settingsInline"><span>Safety</span><strong>Prompt moderation on</strong></div>
      </div>
      <div className="settingsSection"><b>Creative Brain</b><span>{brainProfile.learned_outputs||0} completed outputs analyzed • {brainProfile.feedback_count||0} feedback signals • {(brainProfile.preferred_tags||[]).length} learned preferences.</span>
        <div className="tags">{(brainProfile.preferred_tags||[]).map(t=><span key={t}>{t}</span>)}</div>
        <button onClick={()=>{setShowSettings(false);setShowBrain(true)}}>Open Creative Brain</button>
      </div>
    </div>}
    {settingsTab==='account'&&<div className="settingsGrid">
      <div className="settingsSection"><b>Account</b><span>{token?'Signed in':'Not signed in'} • Your authentication session controls access to generation and account data.</span>
        {supabaseConfigured&&token&&<button onClick={async()=>{await supabase.auth.signOut();setToken('');setShowAuth(true);setStatus('Signed out.')}}>Sign out</button>}
      </div>
      <div className="settingsSection"><b>Credits &amp; billing</b><span>Plans, credit balance and purchases are kept together in the Credits area instead of mixing payment controls into the editing tools.</span>
        <button onClick={()=>{setShowSettings(false);setNav('Billing')}}>Open Credits &amp; Billing</button>
      </div>
    </div>}
    {settingsTab==='advanced'&&<div className="settingsGrid">
      <div className="settingsSection"><b>Providers</b><span>Technical routing information for troubleshooting. Provider secrets never appear in the browser.</span>
        {(providerInfo?.providers||[]).map(p=><div className="providerRow" key={p.key}><div><b>{p.key}</b><small>{p.capability||'generation'}</small></div><span className={p.configured?'providerGood':'providerOff'}>{p.configured?'Configured':'Not configured'}</span></div>)}
        {!providerInfo&&<div className="emptyState"><b>No provider status</b><span>Sign in and connect the production gateway to inspect providers.</span></div>}
      </div>
      <div className="settingsSection"><b>Deployment &amp; safety</b><span>Production code deploys through trusted CI. AI jobs run asynchronously through the gateway with bounded provider routing and moderation.</span>
        <div className="settingsInline"><span>Frontend</span><strong>Cloudflare Worker</strong></div>
        <div className="settingsInline"><span>Gateway</span><strong>Google Cloud Run</strong></div>
        <div className="settingsInline"><span>Provider fallback</span><strong>Automatic</strong></div>
        <div className="settingsInline"><span>Job wait window</span><strong>Up to 15 minutes</strong></div>
      </div>
    </div>}
    <button className="primary" onClick={()=>setShowSettings(false)}>Done</button>
  </div></div>}
  {showExport&&<div className="modalBack"><div className="modal"><div className="modalHead"><b>Export master</b><button onClick={()=>setShowExport(false)}>×</button></div><p>Vidigen renders a real MP4: Android uses the native Media3 exporter; browser builds use the authenticated gateway render worker and durable R2 storage.</p><div className="stats"><div><b>{clips.length}</b><span>clips</span></div><div><b>{captions.length}</b><span>caption segments</span></div><div><b>{ratio}</b><span>aspect</span></div></div><button className="primary" disabled={exportBusy} onClick={exportProject}>{exportBusy?'Rendering…':'Export MP4'}</button></div></div>}
  {showPasswordReset&&<PasswordResetScreen
    onClose={()=>setShowPasswordReset(false)}
    onDone={()=>{setShowPasswordReset(false);setShowAuth(true);}}
  />}
  {showAuth&&<CustomerAuthScreen
    onAuthenticated={(accessToken)=>{setToken(accessToken);setShowAuth(false);}}
    onClose={()=>setShowAuth(false)}
  />}
  <div className="mobileNav">{NAV.slice(0,5).map(([n])=><button key={n} className={nav===n?'active':''} onClick={()=>setNav(n)}>{n}</button>)}</div>
 </div>
}

createRoot(document.getElementById('root')).render(
  SENTRY_DSN ? (
    <Sentry.ErrorBoundary fallback={({error, resetError}) => (
      <div style={{padding: 40, fontFamily: 'sans-serif', color: '#eef0f6', background: '#0d0e13', minHeight: '100vh'}}>
        <h2>Something went wrong.</h2>
        <p style={{color: 'rgba(238,240,246,.6)'}}>This has been reported automatically. Your project data in this browser is unaffected.</p>
        <button onClick={resetError} style={{background: '#4f7cff', color: '#fff', border: 0, borderRadius: 8, padding: '10px 16px', fontWeight: 700, cursor: 'pointer'}}>Try again</button>
      </div>
    )}>
      <App/>
    </Sentry.ErrorBoundary>
  ) : <App/>
);
