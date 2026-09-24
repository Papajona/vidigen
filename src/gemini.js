// Gemini is server-side only. Never put a Gemini API key in VITE_* / the browser bundle.
const DEFAULT_MODEL = 'gemini-2.5-flash';

async function gatewayGemini(base, token, path, body) {
  if (!base) throw new Error('Vidigen gateway URL is not configured.');
  const headers = {'Content-Type':'application/json'};
  if (token) headers.Authorization = `Bearer ${token}`;
  const r = await fetch(`${base.replace(/\/$/,'')}${path}`, {method:'POST', headers, body:JSON.stringify(body)});
  let data = {};
  try { data = await r.json(); } catch {}
  if (!r.ok) throw new Error(data?.detail || `Gemini gateway HTTP ${r.status}`);
  return data;
}

export function geminiConfigured(gateway='') {
  // The gateway URL is runtime state in Vidigen production. Do not require a VITE_* build-time
  // variable when the application already has its canonical gateway configured.
  return Boolean(gateway || import.meta.env.VITE_VIDIGEN_GATEWAY_URL);
}

export async function analyzeWithGemini(prompt, gateway, token) {
  const data = await gatewayGemini(gateway, token, '/api/gemini/analyze', {prompt});
  return data.analysis;
}

export async function improvePromptWithGemini(prompt, mode, gateway, token) {
  const data = await gatewayGemini(gateway, token, '/api/gemini/improve', {prompt, mode});
  return data.prompt;
}
