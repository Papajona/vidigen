const API_KEY = import.meta.env.VITE_GEMINI_API_KEY || (typeof process !== 'undefined' ? process.env?.GEMINI_API_KEY : '');
const MODEL = import.meta.env.VITE_GEMINI_MODEL || 'gemini-2.5-flash';

export function geminiConfigured(){ return Boolean(API_KEY); }

async function callGemini(prompt, json=false){
  if(!API_KEY) throw new Error('Gemini API key is not configured.');
  const url=`https://generativelanguage.googleapis.com/v1beta/models/${encodeURIComponent(MODEL)}:generateContent?key=${encodeURIComponent(API_KEY)}`;
  const body={contents:[{parts:[{text:prompt}]}],generationConfig: json ? {responseMimeType:'application/json'} : {}};
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();
  if(!r.ok) throw new Error(d?.error?.message||`Gemini HTTP ${r.status}`);
  return d?.candidates?.[0]?.content?.parts?.map(p=>p.text||'').join('')||'';
}

export async function analyzeWithGemini(prompt){
  const text=await callGemini(`You are Vidigen AI's production request analyzer. Analyze this media-generation request and return ONLY JSON with keys: type, summary, tags (array of short strings), riskFlags (array), suggestedRatio, suggestedDuration, shotCount, camera, lighting, style. Do not invent unsafe policy violations. Request: ${prompt}`,true);
  return JSON.parse(text);
}

export async function improvePromptWithGemini(prompt, mode){
  return callGemini(`You are an expert cinematic AI director. Rewrite the user's prompt into one production-ready prompt for ${mode}. Preserve the user's intent. Add subject consistency, composition, camera movement, lighting, environment, pacing and negative constraints where useful. Return only the rewritten prompt. User prompt: ${prompt}`);
}
