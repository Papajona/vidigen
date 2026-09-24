"""Content moderation — the gap flagged as a real pre-launch blocker: nothing previously
screened either the input prompt or the generated output before a customer saw it.

Two checks, deliberately different mechanisms for a reason:
- Prompt moderation runs BEFORE billing, so a blocked prompt costs the customer nothing.
  Uses Groq (already integrated for the AI Editor) with the same "ask for strict JSON,
  never trust it blindly" pattern as gateway/edit_ops.py. Falls back to a small keyword
  blocklist if Groq isn't configured — a real, honest fallback (catches the most obvious
  cases), not a fake one that claims coverage it doesn't have.
- Output moderation runs AFTER generation succeeds, before the result is delivered. Uses
  falcons-ai/nsfw_image_detection on Replicate (verified: 78M+ runs, a well-established,
  widely-used model for exactly this) against a representative sampled frame for video, or
  the image directly for image output.

Enforcement policy (VIDIGEN_MODERATION_ENFORCE, default true): when the *automated
classifier itself* cannot run — no API token configured, the provider call fails, the
response is malformed — the old behavior quietly returned `safe: True` ("not enforced for
this item"), which meant moderation coverage silently collapsed to nothing the moment a
token was missing or an upstream API had a bad day. That's a correctness bug for a
public-facing generation product, not an acceptable degraded mode by default.

With enforcement on (the default), "the classifier didn't run" now means `safe: False` —
the item is blocked until it can actually be checked, not waved through. `checked` still
distinguishes "we ran a real classifier and it said no" from "we couldn't check, so we're
refusing to guess" — that distinction is preserved for logging/audit even though both now
result in a block. Enforcement can be explicitly turned off (VIDIGEN_MODERATION_ENFORCE=false)
for local/dev environments that don't have moderation credentials configured and don't want
every generation blocked — this must never be disabled in a production deployment that
serves real users, since it removes the only content check the platform has.
"""
from __future__ import annotations
import json, os, re
import httpx

MODERATION_ENFORCE = os.getenv('VIDIGEN_MODERATION_ENFORCE', 'true').lower() in {'1', 'true', 'yes', 'on'}

# Real, honest limitation: this is a keyword fallback, not a comprehensive classifier. It
# exists to catch obvious cases when the real classifier (Groq) is unavailable, not to
# replace it. Multi-language phrasing, obfuscation (leetspeak, spacing tricks), and anything
# not matching these specific patterns will not be caught here — that gap is the reason
# Groq is the primary path and this is explicitly a degraded fallback, not "the" filter.
BLOCKLIST_PATTERNS = [
    r'\bchild\b.*\b(sex|nude|naked|explicit)\b',
    r'\b(sex|nude|naked|explicit)\b.*\bchild\b',
    r'\bcsam\b',
    r'\b(minor|underage|toddler|infant)\b.*\b(sex|nude|naked|explicit)\b',
    r'\b(sex|nude|naked|explicit)\b.*\b(minor|underage)\b',
    r'\bnon-?consensual\b.*\b(sex|explicit)\b',
    r'\brape\b.*\b(fantasy|scene|video|depict)\b',
    r'\bhow to (make|build|synthesi[sz]e)\b.*\b(bomb|explosive|nerve agent|bioweapon|chemical weapon)\b',
    r'\bhow to (make|synthesi[sz]e|cook)\b.*\b(meth|fentanyl|sarin|ricin)\b',
    r'\bdeepfake\b.*\b(nude|explicit|sex)\b.*\b(without consent|revenge)\b',
]

PROMPT_MODERATION_SYSTEM_PROMPT = (
    'You are a content safety classifier for an AI video/image generation app. Given a '
    'user\'s generation prompt, return ONLY a JSON object: {"safe": true|false, "reason": '
    '"short explanation"}. Mark unsafe: sexual content involving minors in any form, '
    'non-consensual sexual content, content designed to harass or defame a real, named '
    'person, and instructions for making weapons or illegal drugs. Do not mark unsafe for '
    'ordinary creative content, violence in a clearly fictional/cinematic context, or '
    'suggestive-but-non-explicit content. When genuinely unsure, mark safe and explain the '
    'ambiguity — this is a first-pass filter, not the final word.'
)


def _keyword_fallback_check(prompt: str) -> dict:
    lowered = prompt.lower()
    for pattern in BLOCKLIST_PATTERNS:
        if re.search(pattern, lowered):
            return {'safe': False, 'reason': 'Matched a blocked keyword pattern.', 'engine': 'keyword-fallback'}
    return {'safe': True, 'reason': None, 'engine': 'keyword-fallback'}


async def moderate_prompt(prompt: str) -> dict:
    groq_key = os.getenv('GROQ_API_KEY', '')
    if not groq_key:
        return _keyword_fallback_check(prompt)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.post(
                'https://api.groq.com/openai/v1/chat/completions',
                headers={'Authorization': f'Bearer {groq_key}', 'Content-Type': 'application/json'},
                json={
                    'model': os.getenv('GROQ_EDIT_MODEL', 'llama-3.3-70b-versatile'),
                    'messages': [
                        {'role': 'system', 'content': PROMPT_MODERATION_SYSTEM_PROMPT},
                        {'role': 'user', 'content': prompt[:2000]},
                    ],
                    'response_format': {'type': 'json_object'},
                    'temperature': 0,
                    'max_tokens': 200,
                },
            )
        if resp.status_code >= 400:
            return _keyword_fallback_check(prompt)  # Degrade to the weaker check, never fail open silently.
        content = resp.json()['choices'][0]['message']['content']
        parsed = json.loads(content)
        if not isinstance(parsed.get('safe'), bool):
            return _keyword_fallback_check(prompt)  # Malformed model output — don't trust it, fall back.
        return {'safe': parsed['safe'], 'reason': parsed.get('reason'), 'engine': 'groq'}
    except Exception:
        return _keyword_fallback_check(prompt)


NSFW_MODEL = 'falcons-ai/nsfw_image_detection'


def _unchecked_result(reason: str) -> dict:
    """Single choke point for 'the classifier didn't run' outcomes, so the fail-open/
    fail-closed policy lives in exactly one place rather than being repeated (and possibly
    getting out of sync) at every call site that can't reach the classifier."""
    if MODERATION_ENFORCE:
        return {'checked': False, 'safe': False, 'reason': f'{reason} Blocked because output moderation is enforced (VIDIGEN_MODERATION_ENFORCE=true) and could not run.'}
    return {'checked': False, 'safe': True, 'reason': f'{reason} Not enforced (VIDIGEN_MODERATION_ENFORCE=false) — allowed through unchecked.'}


async def moderate_output_image(image_url: str) -> dict:
    token = os.getenv('REPLICATE_API_TOKEN', '')
    if not token:
        return _unchecked_result('REPLICATE_API_TOKEN not configured.')
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(
                f'https://api.replicate.com/v1/models/{NSFW_MODEL}/predictions',
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json', 'Prefer': 'wait=20'},
                json={'input': {'image': image_url}},
            )
        if resp.status_code >= 400:
            return _unchecked_result(f'Moderation model request failed ({resp.status_code}).')
        output = resp.json().get('output')
        label = output if isinstance(output, str) else None
        if label is None:
            return _unchecked_result('Moderation model returned an unrecognized response shape.')
        return {'checked': True, 'safe': label != 'nsfw', 'reason': f'Classifier label: {label}', 'engine': NSFW_MODEL}
    except Exception as e:
        return _unchecked_result(f'Moderation check errored ({e}).')


async def moderate_output_video(video_url: str) -> dict:
    """Samples one representative frame (same ffmpeg-extraction pattern already used in
    gateway/server.py's auto-reframe endpoint) and runs the image classifier on it. This is
    a real, honest limitation worth stating plainly: one frame is not the whole video — a
    brief violation elsewhere in the clip could be missed. A frame-by-frame video
    moderation pass is real additional scope, not a small tweak to this."""
    import subprocess, tempfile, base64
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        source_path = f'{tmp}/source.mp4'
        frame_path = f'{tmp}/frame.jpg'
        try:
            async with httpx.AsyncClient(timeout=60) as c:
                dl = await c.get(video_url)
                if dl.status_code >= 400:
                    return _unchecked_result(f'Could not fetch video for moderation ({dl.status_code}).')
                Path(source_path).write_bytes(dl.content)
            extract = subprocess.run(
                ['ffmpeg', '-y', '-ss', '1', '-i', source_path, '-frames:v', '1', frame_path],
                capture_output=True, timeout=30,
            )
            if extract.returncode != 0 or not Path(frame_path).exists():
                return _unchecked_result('Could not extract a sample frame for moderation.')
            frame_data_url = 'data:image/jpeg;base64,' + base64.b64encode(Path(frame_path).read_bytes()).decode()
        except Exception as e:
            return _unchecked_result(f'Frame extraction for moderation errored ({e}).')
    return await moderate_output_image(frame_data_url)
