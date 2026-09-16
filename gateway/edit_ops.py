"""Validates and sanitizes timeline-edit operations proposed by an LLM (Groq) before they're
ever returned to the client and applied to a real timeline. Kept separate from server.py and
free of any network/LLM calls specifically so it can be unit tested against adversarial and
malformed input without needing a live Groq API key — the same reasoning as reframe.py.

The threat model here isn't "the user is malicious" (they're editing their own timeline) —
it's "the LLM hallucinates or gets confused." An op referencing a clip id that doesn't exist,
a wildly out-of-range speed value, or a garbage `type` string are all things a real model
will occasionally produce, and none of them should reach the client as if they were valid.
"""

ALLOWED_OP_TYPES = {'delete', 'duplicate', 'split', 'trim', 'speed', 'volume'}


def sanitize_ops(raw_ops: object, known_clip_ids: set[str]) -> list[dict]:
    """raw_ops is untrusted, possibly-malformed data straight from an LLM's JSON output —
    could be anything (not even a list, missing fields, wrong types, extra keys). Returns
    only the subset of operations that are well-formed and reference a real clip."""
    if not isinstance(raw_ops, list):
        return []

    clean: list[dict] = []
    for op in raw_ops:
        if not isinstance(op, dict):
            continue
        op_type = op.get('type')
        clip_id = op.get('clipId')
        if op_type not in ALLOWED_OP_TYPES:
            continue
        if not isinstance(clip_id, str) or clip_id not in known_clip_ids:
            continue

        if op_type in ('delete', 'duplicate', 'split'):
            clean.append({'type': op_type, 'clipId': clip_id})

        elif op_type == 'trim':
            try:
                start = max(0.0, float(op.get('trimStart', 0)))
                end = max(0.1, float(op.get('trimEnd', 1)))
            except (TypeError, ValueError):
                continue
            if end <= start:
                continue
            clean.append({'type': 'trim', 'clipId': clip_id, 'trimStart': round(start, 2), 'trimEnd': round(end, 2)})

        elif op_type == 'speed':
            try:
                value = float(op.get('value'))
            except (TypeError, ValueError):
                continue
            clean.append({'type': 'speed', 'clipId': clip_id, 'value': round(max(0.25, min(4.0, value)), 2)})

        elif op_type == 'volume':
            try:
                value = float(op.get('value'))
            except (TypeError, ValueError):
                continue
            clean.append({'type': 'volume', 'clipId': clip_id, 'value': round(max(0.0, min(2.0, value)), 2)})

        # Cap total ops from a single command — a confused model returning 200 ops for
        # "trim this clip" is a bug, not a legitimate large edit.
        if len(clean) >= 20:
            break

    return clean
