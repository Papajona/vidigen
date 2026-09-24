import React, {useEffect, useMemo, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import * as Sentry from '@sentry/react';
import './styles.css';
import {buildPreferenceProfile, buildLocalPrompt, normalizeRequest, validateMemoryRecord} from './learning.mjs';
import {analyzeWithGemini, improvePromptWithGemini, geminiConfigured} from './gemini.js';
import {nativeExportAvailable, exportProjectNative, copyFileToNativeStorage} from './nativeRenderEngine.js';
import {runGooglePayTest} from './googlePayTest.js';
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

const NAV=[['Create','✦'],['Media','▧'],['Text','T'],['Audio','♫'],['Effects','◌'],['Captions','CC'],['Projects','□'],['Billing','¤']];
const MODES=['Text → Video','Image → Video','Video → Video','Text → Image','Commercial Ad','AI Avatar'];
const CAMERA_MOVES=['Auto (let the model choose)','Static shot','Slow push in','Pull out','Pan left','Pan right','Tilt up','Tilt down','Orbit around subject','Handheld','Aerial / drone','Dolly tracking shot'];
const RATIOS=['16:9','9:16','1:1','4:5','21:9'];
const DURATIONS=['4s','5s','8s','10s','15s','30s','60s'];
const MODELS=[['auto','Auto Router'],['replicate','Replicate'],['seedance','Seedance'],['runway','Runway'],['local','ComfyUI']];

const DEFAULT_CLIP={trimStart:0,trimEnd:5,speed:1,volume:1,brightness:100,contrast:100,saturation:100,blur:0,rotation:0,scale:100,opacity:100,keyframes:[]};
const read=(k,f)=>{try{return JSON.parse(localStorage.getItem(k)||'null')??f}catch{return f}};
const save=(k,v)=>localStorage.setItem(k,JSON.stringify(v));

let _onUnauthorizedHandler = null;
const DEFAULT_GATEWAY_FALLBACK='https://vidigen-gateway-xvpegaghzq-uc.a.run.app';
async function gatewayFetch(base,path,options={},token=''){
 const headers={...(options.body instanceof FormData?{}:{'Content-Type':'application/json'}),...(options.headers||{})};
 if(token) headers.Authorization=`Bearer ${token}`;
 const primary=base.replace(/\/$/,'');
 const fallback=(import.meta.env.VITE_VIDIGEN_GATEWAY_FALLBACK_URL||DEFAULT_GATEWAY_FALLBACK).replace(/\/$/,'');
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
       if(retry.ok){ r=retry; base=fallback; }
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

async function startPaystackTest(base,token,amount){
 const r=await gatewayFetch(base,'/api/billing/test/paystack/checkout',{method:'POST',body:JSON.stringify({amount_ghs:amount})},token);
 const d=await r.json(); if(!d.authorization_url) throw new Error('No Paystack TEST checkout URL returned.'); window.location.href=d.authorization_url;
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
 const [gateway,setGateway]=useState(()=>import.meta.env.VITE_VIDIGEN_GATEWAY_URL||DEFAULT_GATEWAY_FALLBACK);
 const [token,setToken]=useState(()=>sessionStorage.getItem('vidigen_gateway_token')||'');
 const [showAuth,setShowAuth]=useState(()=>!sessionStorage.getItem('vidigen_gateway_token'));
 const [showPasswordReset,setShowPasswordReset]=useState(false);
 const [online,setOnline]=useState(false),[providerInfo,setProviderInfo]=useState(null),[status,setStatus]=useState('Ready'),[progress,setProgress]=useState(0);
 const [billing,setBilling]=useState(null),[billingBusy,setBillingBusy]=useState(false);
 const [paymentTest,setPaymentTest]=useState(null),[paymentTestBusy,setPaymentTestBusy]=useState(false);
 const [generating,setGenerating]=useState(false),[history,setHistory]=useState(()=>read('vidigen_learning_history',[]));
 const [projects,setProjects]=useState(()=>read('vidigen_projects',[]));
 const [clips,setClips]=useState(()=>read('vidigen_timeline_v12',[]));
 const [activeId,setActiveId]=useState(()=>read('vidigen_active_clip',null));
 const [memoryOn,setMemoryOn]=useState(()=>localStorage.getItem('vidigen_learning')!=='off');
 const [analysis,setAnalysis]=useState(null),[showBrain,setShowBrain]=useState(false),[showSettings,setShowSettings]=useState(false),[settingsTab,setSettingsTab]=useState('general');
 const [brainProfile,setBrainProfile]=useState({preferred_tags:[],successful_prompts:[],feedback_count:0,learned_outputs:0});
 const [showExport,setShowExport]=useState(false),[exportBusy,setExportBusy]=useState(false),[zoom,setZoom]=useState(1),[bgRemoving,setBgRemoving]=useState(false),[reframing,setReframing]=useState(false);
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
 useEffect(()=>{if(activeClip)setEditor({...DEFAULT_CLIP,...activeClip});},[activeId]); useEffect(()=>{const onKeyDown=e=>{const tag=e.target?.tagName?.toLowerCase();const editing=tag==='input'||tag==='textarea'||tag==='select';const mod=e.ctrlKey||e.metaKey;if(mod&&e.key==='Enter'){e.preventDefault();if(!generating)generate();return}if(mod&&!editing&&e.key.toLowerCase()==='z'){e.preventDefault();e.shiftKey?redo():undo();return}if(mod&&!editing&&e.key.toLowerCase()==='y'){e.preventDefault();redo();return}if(e.key==='/'&&!editing){e.preventDefault();document.querySelector('.prompt')?.focus();}};window.addEventListener('keydown',onKeyDown);return()=>window.removeEventListener('keydown',onKeyDown)},[generating,undoStack.length,redoStack.length]);

 useEffect(()=>{let alive=true;(async()=>{try{
   // /health is intentionally authenticated; use the public /healthz liveness probe to
   // determine whether the production gateway is reachable before a user signs in.
   const h=await gatewayFetch(gateway,'/healthz');
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
 useEffect(()=>{if(nav!=='Billing'||!online)return; let alive=true; (async()=>{try{setBilling(await loadBillingData(gateway,token)); const t=await gatewayFetch(gateway,'/api/billing/test/config',{},token).then(r=>r.json()).catch(()=>null); if(alive)setPaymentTest(t)}catch(e){if(alive)setStatus(e.message)}})(); return()=>{alive=false}},[nav,online,gateway,token]);
 useEffect(()=>{if(!token||!online)return;let alive=true;(async()=>{try{const r=await gatewayFetch(gateway,'/api/brain/profile',{},token);if(alive)setBrainProfile(await r.json())}catch{if(alive)setBrainProfile({preferred_tags:[],successful_prompts:[],feedback_count:0,learned_outputs:0})}})();return()=>{alive=false}},[token,online,gateway,history.length]);
 function snapshot(){setUndoStack(s=>[...s,clips].slice(-30));setRedoStack([])}
 function replaceClips(next){snapshot();setClips(next)}
 function patchClip(p){if(!activeClip)return;const next={...activeClip,...p};setEditor(e=>({...e,...p}));replaceClips(clips.map(c=>c.id===activeClip.id?next:c));}
 function patchClipById(id,p){setClips(cur=>cur.map(c=>c.id===id?{...c,...p}:c))}
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
     const kind=activeClip.kind==='Imported media'&&(activeClip.title||'').match(/\.(png|jpe?g|webp)$/i)?'image':'video'
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
 async function analyze(){setStatus('Analyzing creative brief…');try{const r=await gatewayFetch(gateway,'/api/analyze',{method:'POST',body:JSON.stringify({prompt})},token);setAnalysis(await r.json());setStatus('Brief analyzed.')}catch{try{if(!geminiConfigured())throw 0;setAnalysis(await analyzeWithGemini(prompt,gateway,token));setStatus('Gemini analysis ready.')}catch{setAnalysis(normalizeRequest(prompt));setStatus('Local analysis ready.')}}}
 async function improvePrompt(){setStatus('Optimizing creative direction…');try{if(geminiConfigured()){setPrompt(await improvePromptWithGemini(prompt,mode,gateway,token));}else setPrompt(buildLocalPrompt(prompt,profile,mode));setStatus('Creative brief optimized.')}catch(e){setPrompt(buildLocalPrompt(prompt,profile,mode));setStatus(`Local optimization used: ${e.message}`)}}
 function makeScenes(activeProfile=brainProfile){const total=Math.max(1,parseInt(duration)||5);const count=total>=30?5:total>=15?3:Math.max(1,Math.ceil(total/8));const each=Math.max(2,Math.round((total/count)*10)/10);let learned=memoryOn?buildLocalPrompt(prompt,profile,mode):prompt;if(memoryOn&&(activeProfile?.preferred_tags||[]).length){learned+=` Apply the user's learned creative preferences: ${activeProfile.preferred_tags.join(', ')}.`;}const useAutoCamera=cameraMove===CAMERA_MOVES[0];return Array.from({length:count},(_,i)=>{const motion=useAutoCamera?(i===0?'establishing movement':i===count-1?'controlled closing push-in':'deliberate cinematic movement'):cameraMove;return{id:`scene-${Date.now()}-${i}`,duration:each,prompt:`${learned}. Shot ${i+1} of ${count}; camera direction: ${motion}. Preserve subject identity, lighting, wardrobe, location and visual continuity.`,motion}})}
 async function generate(){if(!token){setShowAuth(true);setStatus('Sign in or create a Vidigen account to generate.');return}const parsed=normalizeRequest(prompt);if(parsed.riskFlags.length){setStatus(`Blocked: ${parsed.riskFlags.join(', ')}`);return}if(!online){setStatus('Gateway offline — connect a provider or local ComfyUI first.');return}setGenerating(true);setProgress(0);let activeProfile=brainProfile;try{if(token&&online){const rp=await gatewayFetch(gateway,'/api/brain/profile',{},token);activeProfile=await rp.json();setBrainProfile(activeProfile)}}catch{}const scenes=makeScenes(activeProfile);try{const out=[];const sourceNeedsUpload=/image\s*→\s*video|video\s*→\s*video/i.test(mode);const generationSource=sourceNeedsUpload&&current?.src?await prepareGenerationSource(current.src):current?.src||null;for(let i=0;i<scenes.length;i++){setStatus(`AI Director • generating shot ${i+1}/${scenes.length}`);const s=scenes[i];const r=await generateScene(gateway,token,{prompt:s.prompt,mode,ratio,duration:`${s.duration}s`,scene:s,sourceUrl:generationSource,model,tags:parsed.tags},st=>setProgress(Math.round(((i+(st.status==='running'?0.5:1))/scenes.length)*100)));out.push({...s,...DEFAULT_CLIP,videoUrl:r.url,src:r.url,jobId:r.jobId,track:'Video',title:`Shot ${i+1}`})}replaceClips([...clips,...out]);setActiveId(out[0].id);const rec={id:Date.now(),prompt,mode,model,jobId:out[0].jobId,timestamp:new Date().toISOString(),rating:0,tags:parsed.tags,success:true};if(validateMemoryRecord(rec))setHistory(h=>[rec,...h].slice(0,500));setProjects(p=>[{id:Date.now(),title:prompt.slice(0,48),mode,date:new Date().toLocaleDateString(),scenes:out.length,clips:out.map(x=>x.id)},...p].slice(0,50));setStatus(`Complete • ${out.length} shot${out.length>1?'s':''} added to timeline.`);setProgress(100)}catch(e){setStatus(e.message)}finally{setGenerating(false)}}
 async function runAICommand(){if(!aiCommand.trim())return;setCommandBusy(true);setStatus('AI Editor is translating your instruction…');try{const r=await gatewayFetch(gateway,'/api/edit-plan',{method:'POST',body:JSON.stringify({command:aiCommand,clips:clips.map(c=>({id:c.id,title:c.title,duration:c.duration,track:c.track,trimStart:c.trimStart,trimEnd:c.trimEnd})),ratio})},token);const plan=await r.json();if(plan.operations?.length){applyOperations(plan.operations);setStatus(`${plan.engine==='groq'?'Groq':'Keyword'} editor applied ${plan.operations.length} timeline operation${plan.operations.length>1?'s':''}.`)}else setStatus('No safe timeline change was identified.');}catch{const q=aiCommand.toLowerCase();if(q.includes('delete')&&activeClip){deleteClip();setStatus('AI Editor deleted the selected clip.')}else if(q.includes('duplicate')&&activeClip){duplicateClip();setStatus('AI Editor duplicated the selected clip.')}else if(q.includes('split')&&activeClip){splitClip()}else setStatus('AI Editor needs the gateway edit planner for this instruction.')}finally{setCommandBusy(false);setAiCommand('')}}
 function applyOperations(ops){let next=[...clips];for(const op of ops){const i=next.findIndex(c=>c.id===op.clipId);if(op.type==='delete'&&i>=0)next.splice(i,1);else if(op.type==='duplicate'&&i>=0)next.splice(i+1,0,{...next[i],id:`clip-${Date.now()}-${i}`,title:`${next[i].title||'Clip'} copy`});else if(op.type==='trim'&&i>=0)next[i]={...next[i],trimStart:Number(op.trimStart??next[i].trimStart),trimEnd:Number(op.trimEnd??next[i].trimEnd)};else if(op.type==='speed'&&i>=0)next[i]={...next[i],speed:Number(op.value)};else if(op.type==='volume'&&i>=0)next[i]={...next[i],volume:Number(op.value)};else if(op.type==='split'&&i>=0){const c=next[i],cut=(Number(c.trimStart||0)+Number(c.trimEnd??c.duration??5))/2;next.splice(i,1,{...c,id:`clip-${Date.now()}-${i}a`,trimEnd:cut,title:`${c.title||'Clip'} A`},{...c,id:`clip-${Date.now()}-${i}b`,trimStart:cut,title:`${c.title||'Clip'} B`})}else if(op.type==='keyframe'&&i>=0)next[i]={...next[i],keyframes:[...(next[i].keyframes||[]),op.keyframe]}}replaceClips(next)}
 async function transcribe(){if(!audio?.blob&&!audio?.file)return;setCaptioning(true);setStatus('Transcribing with the real caption engine…');try{const f=audio.file||audio.blob;const fd=new FormData();fd.append('file',f,audio.name||'audio.webm');const data=await (await gatewayFetch(gateway,'/api/captions',{method:'POST',body:fd},token)).json();setCaptions(data.segments||[]);setStatus(`${(data.segments||[]).length} caption segments created.`)}catch(e){setStatus(e.message)}finally{setCaptioning(false)}}
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
     if(nativeExportAvailable()){
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
       renderClips.push({uri,trimStartMs:Math.round((c.trimStart||0)*1000),trimEndMs:c.trimEnd?Math.round(c.trimEnd*1000):undefined})
     }
     let backgroundAudioUri=audio?.url||undefined
     if(backgroundAudioUri?.startsWith('blob:')){const blob=await fetch(backgroundAudioUri).then(r=>r.blob());backgroundAudioUri=await uploadBlobForRender(blob,`renders/audio-${Date.now()}`,blob.type||'audio/webm')}
     const data=await (await gatewayFetch(gateway,'/api/render',{method:'POST',body:JSON.stringify({ratio,clips:renderClips,backgroundAudioUri:backgroundAudioUri||null})},token)).json()
     if(!data.output_url) throw new Error('Render completed without a durable output URL.')
     setStatus(`Export complete • ${Math.round(data.duration)}s master ready.`); setShowExport(false)
   }catch(e){setStatus(`Export failed: ${e.message}`)}finally{setExportBusy(false)}
 }

 const filter=`brightness(${editor.brightness}%) contrast(${editor.contrast}%) saturate(${editor.saturation}%) blur(${editor.blur}px)`;
 return <div className="app">
  <header className="topbar"><div className="brand"><div className="brandMark">V</div><span>Vidigen</span><b>V12</b></div><div className="projectTitle">AI Production Studio<small>{clips.length} clips • {captions.length} captions • {profile.successCount} learned preferences</small></div><div className="topActions"><span className={`enginePill ${online?'online':''}`}><i/> {online?'Gateway online':'Offline'}</span><button className="ghost" title="Undo (Ctrl/⌘ + Z)" onClick={undo} disabled={!undoStack.length}>Undo</button><button className="ghost" title="Redo (Ctrl/⌘ + Shift + Z)" onClick={redo} disabled={!redoStack.length}>Redo</button><button className="ghost" title="Open Creative Brain" onClick={()=>setShowBrain(true)}>Brain</button><button className="share" onClick={()=>setStatus('Project link sharing is available when persistence/auth is configured.')}>Share</button><button className="export" onClick={()=>setShowExport(true)}>Export</button>{!token&&<button className="ghost" onClick={()=>setShowAuth(true)}>Sign in</button>}<div className="avatar">JA</div></div></header>
  <div className="editor">
   <nav className="rail">{NAV.map(([n,icon])=><button key={n} className={`railItem ${nav===n?'active':''}`} onClick={()=>setNav(n)}><strong>{icon}</strong><span>{n}</span></button>)}<button className="railItem" onClick={()=>setShowSettings(true)}><strong>⚙</strong><span>Settings</span></button></nav>
   <main className="center"><div className="previewHeader"><div><b>{mode}</b><span>{ratio} • {duration}</span></div><div className="previewActions"><button onClick={()=>{if(video.current)video.current.currentTime=Math.max(0,video.current.currentTime-1)}}>−1s</button><button onClick={()=>video.current?.play()}>Play</button><button onClick={()=>video.current?.pause()}>Pause</button><button onClick={()=>{if(video.current)video.current.currentTime+=1}}>+1s</button></div></div>
    <div className="stage"><div className={`videoFrame ratio-${ratio.replace(':','-').replace('.','-')}`} style={{filter}}>{current?.src&&<video ref={video} src={current.src} controls={false} playsInline onTimeUpdate={()=>{}}/>}<button className="bigPlay" onClick={()=>video.current?.paused?video.current?.play():video.current?.pause()}>▶</button>{captions[0]&&<div className={`captionOverlay ${captionStyle.toLowerCase()}`}>{captions[0].text}</div>}{activeClip?.overlay?.text&&<div className="customOverlay" style={{left:`${activeClip.overlay.x}%`,top:`${activeClip.overlay.y}%`,fontSize:`${activeClip.overlay.size}px`,fontWeight:activeClip.overlay.bold?800:500}}>{activeClip.overlay.text}</div>}<div className="stageInfo"><span>{activeClip?.title||'Preview'}</span><span>{activeClip?.keyframes?.length||0} keyframes</span></div></div></div>
    <div className="transport"><button onClick={()=>{if(video.current)video.current.currentTime=0}}>⏮</button><button onClick={()=>video.current?.paused?video.current?.play():video.current?.pause()}>▶/Ⅱ</button><button onClick={()=>{if(video.current)video.current.currentTime=video.current.duration||0}}>⏭</button><div className="scrub" onClick={e=>{if(!video.current?.duration)return;video.current.currentTime=(e.nativeEvent.offsetX/e.currentTarget.clientWidth)*video.current.duration}}><div style={{width:`${video.current?.duration?((video.current.currentTime/video.current.duration)*100):0}%`}}/></div><span>{status}{generating?` • ${progress}%`:''}</span></div>
   </main>
   <aside className="inspector">
    {nav==='Create'&&<><div className="inspectorTop"><b>AI Director</b><span className="tinyBadge">production workflow</span></div><div className="modeGrid">{MODES.map(m=><button key={m} className={`mode ${mode===m?'active':''}`} onClick={()=>setMode(m)}>{m}</button>)}</div><label>Creative brief</label><textarea className="prompt" value={prompt} onChange={e=>setPrompt(e.target.value)} placeholder="Describe the story, subject, style, motion, audience and CTA…"/><div className="promptMeta"><span>AI Director brief</span><kbd>Ctrl/⌘ + Enter</kbd></div><div className="promptChips"><button type="button" onClick={()=>{setPrompt('Create a premium cinematic product ad with a bold opening hook, elegant close-ups, controlled camera motion and a clear final CTA.');setMode('Commercial Ad')}}>Product ad</button><button type="button" onClick={()=>{setPrompt('Create a fast, energetic vertical social video with a strong 2-second hook, punchy cuts, captions and a memorable ending.');setRatio('9:16')}}>Social short</button><button type="button" onClick={()=>setPrompt('Create a cinematic story with consistent character identity, dramatic lighting, rich composition, deliberate camera movement and a satisfying visual payoff.')}>Cinematic</button></div><div className="promptActions"><button onClick={analyze}>Analyze</button><button onClick={improvePrompt}>Optimize</button></div>{analysis&&<div className="analysis"><b>{analysis.type||'video'}</b><span>{analysis.summary||'Ready for production.'}</span><small>{(analysis.tags||[]).join(' • ')}</small></div>}<div className="twoFields"><div><label>Aspect ratio</label><select value={ratio} onChange={e=>setRatio(e.target.value)}>{RATIOS.map(x=><option key={x}>{x}</option>)}</select></div><div><label>Duration</label><select value={duration} onChange={e=>setDuration(e.target.value)}>{DURATIONS.map(x=><option key={x}>{x}</option>)}</select></div></div>{activeClip&&<button disabled={reframing} onClick={autoReframe}>{reframing?'Reframing…':`Auto-reframe selected clip to ${ratio}`}</button>}<label>Camera direction</label>{activeClip&&<p className="hint">Analyzes one frame to find the subject, then applies a single fixed crop to the whole clip — not full per-frame tracking, so fast-moving subjects can drift toward the edge on longer clips.</p>}<select value={cameraMove} onChange={e=>setCameraMove(e.target.value)}>{CAMERA_MOVES.map(x=><option key={x}>{x}</option>)}</select><label>Model routing</label><select value={model} onChange={e=>setModel(e.target.value)}>{MODELS.map(([id,n])=><option key={id} value={id}>{n}</option>)}</select><div className="directorCard"><span>SHOT PLAN</span><b>{duration==='30s'||duration==='60s'?'multi-shot':'single-shot'}</b><p>{cameraMove===CAMERA_MOVES[0]?'Camera direction is chosen automatically per shot. Pick a specific move above for direct control.':`Every shot in this sequence uses: ${cameraMove}.`} Continuity across shots (identity, lighting, wardrobe) is requested in the prompt text sent to the model — it isn't independently verified or enforced by this app.</p></div><button className="generate" disabled={generating} onClick={generate}>{generating?`Generating ${progress}%`:'Create production'}<small>Generate → evaluate → assemble on timeline</small></button><div className="sectionCard"><b>AI Editor</b><span>Describe a timeline change in natural language. Uses Groq for real language understanding when configured on the gateway, bounded to a fixed safe set of operations (trim/split/delete/duplicate/speed/volume) either way — falls back to simple keyword matching otherwise.</span><input value={aiCommand} onChange={e=>setAiCommand(e.target.value)} placeholder="e.g. trim the selected clip to 3s"/><button onClick={runAICommand} disabled={commandBusy||!aiCommand.trim()}>{commandBusy?'Planning…':'Apply AI edit'}</button></div><div className="sectionCard"><b>Creative memory</b><span>Remembers tags and styles from your past generations to nudge future prompts — a local preference tracker, not model training.</span><button className={`toggle ${memoryOn?'on':''}`} onClick={()=>setMemoryOn(x=>!x)}>{memoryOn?'Remembering your preferences: ON':'Remembering your preferences: OFF'}</button></div></>}
    {nav==='Media'&&<div className="sectionCard"><b>Media library</b><p>Import your own production footage or images into the timeline.</p><input type="file" accept="video/*,image/*" onChange={e=>{const f=e.target.files?.[0];if(f){const c={id:`media-${Date.now()}`,title:f.name,kind:'Imported media',src:URL.createObjectURL(f),track:'Video',duration:5,...DEFAULT_CLIP};replaceClips([...clips,c]);setActiveId(c.id);copyFileToNativeStorage(f).then(nativeUri=>{if(nativeUri)patchClipById(c.id,{nativeUri})}).catch(()=>{})}}}/>{clips.length?<div className="mediaImported"><b>{clips.length} production asset(s) in this project</b><small>Assets are project-scoped and come only from this project or its AI generation jobs.</small></div>:<div className="emptyState"><b>No media imported yet</b><span>Upload production footage or generate new assets with AI Director.</span></div>}</div>}
    {nav==='Text'&&<><div className="sectionCard"><b>Text & motion graphics</b><label>Overlay</label><input value={overlay.text} onChange={e=>setOverlay({...overlay,text:e.target.value})}/><div className="twoFields"><div><label>Size</label><input type="number" min="12" max="120" value={overlay.size} onChange={e=>setOverlay({...overlay,size:+e.target.value})}/></div><div><label>Y</label><input type="number" min="5" max="95" value={overlay.y} onChange={e=>setOverlay({...overlay,y:+e.target.value})}/></div></div><button onClick={addOverlay}>Attach to selected clip</button></div><div className="sectionCard"><b>AI Presenter</b><label>Script</label><textarea className="prompt" value={avatar.script} onChange={e=>setAvatar({...avatar,script:e.target.value})}/><div className="twoFields"><select value={avatar.presenter} onChange={e=>setAvatar({...avatar,presenter:e.target.value})}><option>Studio Presenter</option><option>Business Presenter</option><option>Creator Presenter</option></select><select value={avatar.voice} onChange={e=>setAvatar({...avatar,voice:e.target.value})}><option>Natural</option><option>Warm</option><option>Confident</option></select></div><button onClick={()=>{setMode('AI Avatar');setPrompt(`${avatar.script} Presenter: ${avatar.presenter}. Voice: ${avatar.voice}. Language: ${avatar.language}. Background: ${avatar.background}.`);setNav('Create');setStatus('Avatar production brief prepared.')}}>Prepare avatar</button></div></>}
    {nav==='Audio'&&<><div className="sectionCard"><b>Audio studio</b><input ref={audioInput} type="file" accept="audio/*,video/*" onChange={e=>importAudio(e.target.files?.[0])}/><div className="audioButtons"><button onClick={recording?stopRecording:startRecording}>{recording?'Stop recording':'Record voiceover'}</button><button onClick={transcribe} disabled={!audio||captioning}>{captioning?'Transcribing…':'Auto captions'}</button></div>{audio&&<audio controls src={audio.url}/>}</div><div className="sectionCard"><b>Selected clip mix</b>{activeClip&&<><label>Volume {Math.round(editor.volume*100)}%</label><input type="range" min="0" max="2" step=".01" value={editor.volume} onChange={e=>patchClip({volume:+e.target.value})}/><label>Speed {editor.speed}×</label><input type="range" min=".25" max="4" step=".05" value={editor.speed} onChange={e=>patchClip({speed:+e.target.value})}/></>}</div></>}
    {nav==='Effects'&&<><div className="sectionCard"><b>Professional controls</b><div className="twoFields"><div><label>Trim in</label><input type="number" min="0" step=".1" value={editor.trimStart} onChange={e=>patchClip({trimStart:+e.target.value})}/></div><div><label>Trim out</label><input type="number" min="0" step=".1" value={editor.trimEnd} onChange={e=>patchClip({trimEnd:+e.target.value})}/></div></div><div className="twoFields"><div><label>Brightness</label><input type="range" min="50" max="150" value={editor.brightness} onChange={e=>patchClip({brightness:+e.target.value})}/></div><div><label>Contrast</label><input type="range" min="50" max="150" value={editor.contrast} onChange={e=>patchClip({contrast:+e.target.value})}/></div></div><div className="twoFields"><div><label>Saturation</label><input type="range" min="0" max="200" value={editor.saturation} onChange={e=>patchClip({saturation:+e.target.value})}/></div><div><label>Blur</label><input type="range" min="0" max="12" value={editor.blur} onChange={e=>patchClip({blur:+e.target.value})}/></div></div><label>Scale {editor.scale}%</label><input type="range" min="50" max="150" value={editor.scale} onChange={e=>patchClip({scale:+e.target.value})}/><label>Opacity {editor.opacity}%</label><input type="range" min="0" max="100" value={editor.opacity} onChange={e=>patchClip({opacity:+e.target.value})}/><label>Rotation {editor.rotation}°</label><input type="range" min="-180" max="180" value={editor.rotation} onChange={e=>patchClip({rotation:+e.target.value})}/><div className="audioButtons"><button onClick={splitClip}>Split</button><button onClick={duplicateClip}>Duplicate</button><button onClick={deleteClip}>Delete</button></div><div className="audioButtons"><button disabled={bgRemoving||!activeClip} onClick={removeBackground}>{bgRemoving?'Removing background…':'Remove background'}</button></div><div className="audioButtons"><button disabled={bgRemoving||!activeClip} onClick={enhancePhoto}>{bgRemoving?'Enhancing…':'Enhance selected photo'}</button></div>{activeClip&&<p className="hint">Background removal uploads this clip to your configured Cloudflare R2 bucket, then runs it through Replicate — needs R2 and REPLICATE_API_TOKEN set on the gateway.</p>}</div><div className="sectionCard"><b>Keyframes</b><p>Animate transform properties directly on the timeline.</p><button onClick={addKeyframe}>Add keyframe at playhead</button><button onClick={clearKeyframes}>Clear keyframes</button>{activeClip?.keyframes?.map((k,i)=><div className="keyframeRow" key={i}><span>{k.time.toFixed(2)}s</span><small>scale {k.scale}% • opacity {k.opacity}% • rot {k.rotation}°</small></div>)}</div></>}
    {nav==='Captions'&&<><div className="sectionCard"><b>Caption studio</b><p>{captions.length?`${captions.length} timed segments ready.`:'No timed captions yet.'}</p><button onClick={transcribe} disabled={!audio||captioning}>{captioning?'Transcribing…':'Transcribe audio'}</button><button onClick={downloadSrt}>Export SRT</button><label>Style</label><select value={captionStyle} onChange={e=>setCaptionStyle(e.target.value)}><option>Bold</option><option>Clean</option><option>Minimal</option></select></div><div className="captionList">{captions.slice(0,40).map((c,i)=><div key={i}><time>{c.start.toFixed(2)}s</time><span>{c.text}</span></div>)}</div></>}
    {nav==='Billing'&&<div className="sectionCard"><b>Vidigen Plans &amp; Credits</b><p>Subscriptions use monthly credits so premium video generations remain cost-controlled. Prices and credit budgets are managed server-side by an administrator.</p>{billing?.subscription&&<div className="analysis"><b>{billing.subscription.plan_slug}</b><span>Active until {new Date(billing.subscription.current_period_end).toLocaleDateString()}</span></div>}<div className="cards">{(billing?.plans||[]).map(p=><div className="card" key={p.slug}><b>{p.name}</b><strong>{p.price_ghs===0?'Free':`GHS ${Number(p.price_ghs).toFixed(0)}/month`}</strong><small>{p.monthly_credits.toLocaleString()} credits • {p.storage_gb} GB{p.watermark?' • watermark':''}{p.commercial_use?' • commercial':''}</small>{p.slug==='free'?<button disabled>Current free tier</button>:<div className="audioButtons"><button disabled={billingBusy} onClick={async()=>{setBillingBusy(true);try{await startBillingCheckout(gateway,token,p.slug,'paystack')}catch(e){setStatus(e.message)}finally{setBillingBusy(false)}}}>{billingBusy?'Opening…':'Pay with Paystack'}</button><button disabled title="Google Pay production is separate and remains disabled until Google approval and a confirmed production processor are configured.">Google Pay (pending)</button></div>}</div>)}</div><div className="hint">Free plan: 5 avatar generations and 5 photo enhancements per day, with 500 MB storage. Video generation is available through purchased credits or a paid plan.</div><div className="analysis" style={{marginTop:12}}><b>Video credits</b><span>150 credits • GHS 30.00</span><button onClick={async()=>{try{const r=await gatewayFetch(gateway,'/api/billing/checkout/credits',{method:'POST',body:JSON.stringify({credits:150})},token);const d=await r.json();if(!d.authorization_url)throw new Error('No Paystack checkout URL returned.');window.location.href=d.authorization_url}catch(e){setStatus(e.message)}}}>Buy 150 credits</button></div><div className="analysis"><b>Credit balance</b><span>{billing?.wallet?.balance??'—'} credits remaining</span></div><div className="sectionCard" style={{marginTop:16}}><b>Payment Integration TEST</b><p>Use only GHS 10, GHS 20 and GHS 50. Paystack TEST must use a <code>sk_test_</code> key. Successful test transactions never grant a production subscription.</p><div className="cards">{[10,20,50].map(amount=><div className="card" key={amount}><b>GHS {amount}.00</b><small>Payment integration test</small><div className="audioButtons"><button disabled={paymentTestBusy||!paymentTest?.paystack_test_key_configured} onClick={async()=>{setPaymentTestBusy(true);try{await startPaystackTest(gateway,token,amount)}catch(e){setStatus(e.message);setPaymentTestBusy(false)}}}>Paystack TEST</button><button disabled={paymentTestBusy||paymentTest?.google_pay_test_mode===false} onClick={async()=>{setPaymentTestBusy(true);setStatus(`Opening Google Pay TEST for GHS ${amount}.00…`);try{const result=await runGooglePayTest(amount);setStatus(`Google Pay TEST succeeded for GHS ${amount}.00. No live charge was made.`);console.log('Google Pay TEST paymentData',result.paymentData)}catch(e){setStatus(`Google Pay TEST: ${e.message}`)}finally{setPaymentTestBusy(false)}}}>Google Pay TEST</button></div></div>)}</div><div className="hint">Google Pay TEST success means Google Pay returned test payment data to the browser. It is not proof of a live charge or proof that Paystack is the production Google Pay gateway.</div></div></div>}
    {nav==='Projects'&&<div className="sectionCard"><b>Projects</b><p>{projects.length} production records. Account/project persistence is used when the production backend is configured.</p>{projects.map(p=><button className="mediaLine" key={p.id} onClick={()=>setStatus(`Project: ${p.title}`)}><div className="projectThumb">AI</div><span>{p.title}<small>{p.mode} • {p.scenes} shots</small></span></button>)}</div>}
   </aside>
  </div>
  <section className="timelinePanel"><div className="timelineTop"><div><b>Timeline</b><span>{clips.length} clips • {Math.round(clips.reduce((a,c)=>a+Math.max(0,Number(c.trimEnd??c.duration??5)-Number(c.trimStart||0)),0)*10)/10}s</span></div><div className="timelineButtons"><button onClick={()=>moveClip(-1)}>←</button><button onClick={()=>moveClip(1)}>→</button><button onClick={splitClip}>Split</button><button onClick={duplicateClip}>Duplicate</button><button onClick={deleteClip}>Delete</button><button onClick={()=>setZoom(z=>Math.min(2,z+.1))}>Zoom +</button><button onClick={()=>setZoom(z=>Math.max(.5,z-.1))}>Zoom −</button></div></div><div className="timelineBody"><div className="labels"><span>VIDEO</span><span>OVERLAY</span><span>TEXT</span><span>AUDIO</span></div><div className="tracks" style={{'--zoom':zoom}}><div className="ruler">00:00　　00:05　　00:10　　00:15　　00:20　　00:25　　00:30</div><div className="row videoRow">{clips.filter(c=>c.track!=='Audio').map((c,i)=><button key={c.id} className={`clip ${activeClip?.id===c.id?'selected':''}`} style={{width:`${Math.max(110,(Number(c.trimEnd??c.duration??5)-Number(c.trimStart||0))*34*zoom)}px`}} onClick={()=>{setActiveId(c.id);setNav('Effects')}}><span>{c.title||`Shot ${i+1}`}</span><small>{Math.max(0,Number(c.trimEnd??c.duration??5)-Number(c.trimStart||0)).toFixed(1)}s</small></button>)}</div><div className="row"><div className="emptyClip">Overlay track • attach text, masks or graphics</div></div><div className="row"><div className="textClip">{captions.length?`CC • ${captions.length} timed segments`:'Text / captions track'}</div></div><div className="row"><div className="audioClip">{audio?`♫ ${audio.name}`:'Audio / music / SFX track'}</div></div><div className="playhead"/></div></div></section>
  <section className="bottom"><div className="bottomHead"><b>Production assets</b><span>{clips.length} asset(s) in this project</span></div>{clips.length?<div className="cards">{clips.slice(0,12).map((c,i)=><button className="card" key={c.id} onClick={()=>{setActiveId(c.id);setNav('Effects')}}>{c.src?.startsWith('blob:')?<video src={c.src} muted/>:<div className="assetPlaceholder">AI</div>}<span><b>{c.title||`Shot ${i+1}`}</b><small>{c.kind||'Production asset'}</small></span></button>)}</div>:<div className="emptyState"><b>Your production assets will appear here</b><span>Generate or import media to begin.</span></div>}</section>
  <div className="feedback"><span>Teach the Brain from the latest result</span><button onClick={()=>rate(5)}>★ Excellent</button><button onClick={()=>rate(3)}>Good</button><button onClick={()=>rate(1)}>Needs work</button><button onClick={()=>setShowBrain(true)}>View Brain</button></div>
  {showBrain&&<div className="modalBack"><div className="modal wideModal"><div className="modalHead"><div><b>Vidigen Creative Brain</b><small className="modalSub">Persistent preference learning and output memory</small></div><button onClick={()=>setShowBrain(false)}>×</button></div><div className="stats"><div><b>{brainProfile.learned_outputs||history.length}</b><span>learned outputs</span></div><div><b>{brainProfile.feedback_count||0}</b><span>feedback signals</span></div><div><b>{(brainProfile.preferred_tags||profile.preferredTags||[]).length}</b><span>style preferences</span></div></div><div className="brainStatus"><span className="statusDot"/><div><b>{online?'Brain connected to server':'Local Brain only'}</b><small>{online?'Completed outputs and feedback can persist to your Vidigen account.':'Sign in and connect the production gateway to enable persistent server learning.'}</small></div></div><label>Learned style signals</label><div className="tags">{(brainProfile.preferred_tags||profile.preferredTags||[]).length?(brainProfile.preferred_tags||profile.preferredTags).map(t=><span key={t}>{t}</span>):<small>No high-confidence preferences yet. Rate completed work to teach the Brain.</small>}</div><label>How learning works</label><p className="hint">Each completed job can be analyzed into reusable creative metadata. Only explicit positive feedback influences preferred style signals. Vidigen does not silently retrain third-party foundation models.</p><button className="danger" onClick={()=>{setHistory([]);setBrainProfile({preferred_tags:[],successful_prompts:[],feedback_count:0,learned_outputs:0});setStatus('Local creative memory cleared. Server Brain data remains account-scoped.')}}>Clear local memory</button></div></div>}
  {showSettings&&<div className="modalBack"><div className="modal wideModal"><div className="modalHead"><div><b>Studio Settings</b><small className="modalSub">Control your workspace, AI routing and account</small></div><button onClick={()=>setShowSettings(false)}>×</button></div><div className="settingsTabs"><button className={settingsTab==='general'?'active':''} onClick={()=>setSettingsTab('general')}>General</button><button className={settingsTab==='ai'?'active':''} onClick={()=>setSettingsTab('ai')}>AI & Providers</button><button className={settingsTab==='brain'?'active':''} onClick={()=>setSettingsTab('brain')}>Creative Brain</button><button className={settingsTab==='account'?'active':''} onClick={()=>setSettingsTab('account')}>Account</button><button className={settingsTab==='storage'?'active':''} onClick={()=>setSettingsTab('storage')}>Storage</button><button className={settingsTab==='deployment'?'active':''} onClick={()=>setSettingsTab('deployment')}>Deployment</button></div>{settingsTab==='general'&&<div className="settingsGrid"><div className="settingsSection"><b>Workspace</b><span>Use the production studio with the same project state and timeline controls on desktop and mobile.</span><label>Gateway URL</label><input value={gateway} onChange={e=>setGateway(e.target.value)} placeholder="https://api.vidigen.online"/><div className="settingsInline"><span>Connection</span><strong className={online?'okText':'badText'}>{online?'Online':'Offline'}</strong></div><div className="settingsInline"><span>Interface density</span><strong>Studio</strong></div></div><div className="settingsSection"><b>Keyboard shortcuts</b><span>Ctrl/⌘ + Enter generate • Ctrl/⌘ + Z undo • Ctrl/⌘ + Shift + Z redo • / focus AI brief.</span><span>These shortcuts stay local to the browser and do not transmit keypresses.</span></div></div>}{settingsTab==='ai'&&<div className="settingsGrid"><div className="settingsSection"><b>Provider routing</b><span>Secrets stay server-side. The browser receives capability/configuration status only.</span>{(providerInfo?.providers||[]).map(p=><div className="providerRow" key={p.key}><div><b>{p.key}</b><small>{p.capability||'generation'}</small></div><span className={p.configured?'providerGood':'providerOff'}>{p.configured?'Configured':'Not configured'}</span></div>)}{!providerInfo&&<div className="emptyState"><b>No provider status yet</b><span>Connect the production gateway to inspect available providers.</span></div>}</div><div className="settingsSection"><b>Safety & quality</b><span>Prompt moderation runs before generation. Completed outputs are checked before delivery when moderation enforcement is enabled.</span><div className="settingsInline"><span>Output auto-learning</span><strong>Server-side</strong></div><div className="settingsInline"><span>Provider fallback</span><strong>Automatic</strong></div><p className="hint">The gateway tries configured providers in priority order and can continue on another provider when an accepted job later fails. The logical request is billed once.</p></div></div>}{settingsTab==='brain'&&<div className="settingsGrid"><div className="settingsSection"><b>Creative Brain</b><span>{brainProfile.learned_outputs||0} completed outputs analyzed • {brainProfile.feedback_count||0} feedback signals • {(brainProfile.preferred_tags||[]).length} learned style preferences.</span><div className="tags">{(brainProfile.preferred_tags||[]).map(t=><span key={t}>{t}</span>)}</div></div><div className="settingsSection"><b>Learning boundary</b><span>Vidigen currently learns preferences, prompt patterns and output metadata. It does not update the weights of Replicate, Seedance, Runway or other third-party foundation models.</span><span>Imported resources should be user-owned or appropriately licensed and retain provenance.</span><button onClick={()=>setShowBrain(true)}>Open Brain dashboard</button></div></div>}{settingsTab==='account'&&<div className="settingsGrid"><div className="settingsSection"><b>Authentication</b><span>{token?'Signed in':'Not signed in'} • Supabase session controls the production gateway access.</span>{supabaseConfigured&&token&&<button onClick={async()=>{await supabase.auth.signOut();setToken('');setShowAuth(true);setStatus('Signed out.')}}>Sign out</button>}<div className="settingsInline"><span>Leaked password protection</span><strong className="providerOff">Requires Supabase Pro+</strong></div></div><div className="settingsSection"><b>Admin security</b><span>Admin actions use a separate TOTP session layer when 2FA is enrolled.</span><span>Never paste a service-role key or provider secret into browser settings.</span></div></div>}{settingsTab==='storage'&&<div className="settingsGrid"><div className="settingsSection"><b>Plan storage</b><span>{billing?.wallet?'Your account is connected to server-side billing.':'Open Billing to load your current plan.'}</span><div className="stats"><div><b>{billing?.subscription?.plan_slug||'Free'}</b><span>plan</span></div><div><b>{billing?.plans?.find(p=>p.slug===(billing?.subscription?.plan_slug||'free'))?.storage_gb||0.5} GB</b><span>quota</span></div><div><b>{brainProfile.learned_outputs||0}</b><span>learned outputs</span></div></div></div><div className="settingsSection"><b>Media policy</b><span>Large media should live in durable R2 storage. The database stores metadata and learning records, not video blobs.</span><span>Current browser-only imports remain local until uploaded for a generation or export.</span></div></div>}{settingsTab==='deployment'&&<div className="settingsGrid"><div className="settingsSection"><b>Trusted auto-deploy</b><span>Code changes pushed to <strong>main</strong> trigger the GitHub Actions deployment pipelines after automated tests pass.</span><div className="settingsInline"><span>Frontend</span><strong>Cloudflare Worker</strong></div><div className="settingsInline"><span>Gateway</span><strong>Google Cloud Run</strong></div><div className="settingsInline"><span>Deployment credentials</span><strong>Keyless WIF</strong></div></div><div className="settingsSection"><b>AI job execution</b><span>Provider jobs are asynchronous. The gateway records the external job ID and, when available, the provider status endpoint, then polls until completion.</span><div className="settingsInline"><span>Auto Router</span><strong>Server-controlled</strong></div><div className="settingsInline"><span>Agent wait window</span><strong>Up to 15 minutes</strong></div><p className="hint">The app does not grant the AI agent unrestricted shell or GitHub deployment access. Production code deployment remains a trusted CI operation, while the Agent can orchestrate generation, QA and learning inside bounded tools.</p></div></div>}<button className="primary" onClick={()=>setShowSettings(false)}>Done</button></div></div>}
  {showExport&&<div className="modalBack"><div className="modal"><div className="modalHead"><b>Export master</b><button onClick={()=>setShowExport(false)}>×</button></div><p>Vidigen renders a real MP4: Android uses the native Media3 exporter; browser builds use the authenticated gateway render worker and durable R2 storage.</p><div className="stats"><div><b>{clips.length}</b><span>clips</span></div><div><b>{captions.length}</b><span>caption segments</span></div><div><b>{ratio}</b><span>aspect</span></div></div><button className="primary" disabled={exportBusy} onClick={exportProject}>{exportBusy?'Rendering…':'Export MP4'}</button></div></div>}
  {showAuth&&<CustomerAuthScreen
    onAuthenticated={(accessToken)=>{setToken(accessToken);setShowAuth(false);setStatus('Signed in successfully.');}}
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
