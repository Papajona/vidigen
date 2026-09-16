const TAGS = [
  'cinematic','commercial','portrait','product','landscape','slow','dynamic','studio',
  'dramatic','natural','luxury','minimal','anime','documentary','fashion','travel',
  'food','realistic','editorial','social','vertical','widescreen'
];

export function normalizeRequest(input) {
  const text = String(input ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return { text: '', type: 'empty', tags: [], riskFlags: [] };
  const lower = text.toLowerCase();
  let type = 'video';
  if (/text\s*(to|→)\s*image|generate.*image|image generation/.test(lower)) type = 'text-to-image';
  else if (/image\s*(to|→)\s*video|animate.*image/.test(lower)) type = 'image-to-video';
  else if (/video\s*(to|→)\s*video|restyle.*video|edit.*video/.test(lower)) type = 'video-to-video';
  else if (/avatar|talking head|digital human/.test(lower)) type = 'avatar';
  else if (/ad|advert|commercial|campaign/.test(lower)) type = 'commercial';
  else if (/caption|subtitle|transcri/.test(lower)) type = 'caption';
  const tags = TAGS.filter(t => lower.includes(t));
  const riskFlags = [];
  if (/password|api key|secret|private key|token\b/.test(lower)) riskFlags.push('credential_request');
  if (/bypass|exploit|malware|ransomware|steal|phish/.test(lower)) riskFlags.push('abuse_request');
  if (/copyright|pirated|torrent|remove watermark/.test(lower)) riskFlags.push('copyright_risk');
  return { text, type, tags, riskFlags };
}

export function buildPreferenceProfile(history) {
  const rows = Array.isArray(history) ? history : [];
  const liked = rows.filter(x => Number(x.rating) >= 4);
  const tagCounts = new Map();
  for (const row of liked) for (const tag of (row.tags || [])) tagCounts.set(tag, (tagCounts.get(tag) || 0) + 1);
  const preferredTags = [...tagCounts.entries()].sort((a,b)=>b[1]-a[1]).slice(0,8).map(([tag])=>tag);
  const successfulPrompts = liked.slice(0,10).map(x => x.prompt).filter(Boolean);
  return { preferredTags, successfulPrompts, successCount: liked.length };
}

export function buildLocalPrompt(prompt, profile, mode) {
  const p = normalizeRequest(prompt);
  if (p.riskFlags.length) throw new Error(`Request blocked by local safety policy: ${p.riskFlags.join(', ')}`);
  const prefs = profile?.preferredTags?.length ? ` User preference memory: ${profile.preferredTags.join(', ')}.` : '';
  return `${p.text}. Create a polished ${String(mode || 'video').toLowerCase()} with consistent subject identity, deliberate camera motion, clean composition, cinematic lighting, natural motion, and no text or watermarks.${prefs}`;
}

export function validateMemoryRecord(record) {
  if (!record || typeof record !== 'object') return false;
  return typeof record.prompt === 'string' && record.prompt.length <= 4000 &&
    Number.isInteger(Number(record.rating)) && Number(record.rating) >= 0 && Number(record.rating) <= 5;
}
