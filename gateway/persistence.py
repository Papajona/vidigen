"""Optional Supabase/R2 persistence. The local gateway still works when these are not configured."""
import os, time, httpx

SUPABASE_URL=os.getenv('SUPABASE_URL','').rstrip('/')
SUPABASE_SERVICE_ROLE_KEY=os.getenv('SUPABASE_SERVICE_ROLE_KEY','')

class PersistenceError(RuntimeError): pass

def enabled(): return bool(SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY)

async def sb_request(method,path,json_body=None,params=None):
    if not enabled(): return None
    headers={'apikey':SUPABASE_SERVICE_ROLE_KEY,'Authorization':f'Bearer {SUPABASE_SERVICE_ROLE_KEY}','Content-Type':'application/json','Prefer':'return=representation'}
    async with httpx.AsyncClient(timeout=20,follow_redirects=False) as c:
        r=await c.request(method,f'{SUPABASE_URL}/rest/v1/{path}',headers=headers,json=json_body,params=params)
    if r.status_code>=400: raise PersistenceError(f'Supabase request failed ({r.status_code}).')
    return r.json() if r.content else []

async def create_job(user_id, project_id, provider, model, request):
    rows=await sb_request('POST','generation_jobs',{'user_id':user_id,'project_id':project_id,'provider':provider,'model':model,'request':request,'status':'queued'})
    return rows[0]['id'] if rows else None

async def update_job(job_id, **fields):
    if not enabled(): return
    await sb_request('PATCH','generation_jobs',fields,params={'id':f'eq.{job_id}'})

async def get_job(job_id,user_id=None):
    params={'id':f'eq.{job_id}','select':'*','limit':'1'}
    if user_id: params['user_id']=f'eq.{user_id}'
    rows=await sb_request('GET','generation_jobs',params=params)
    return rows[0] if rows else None

async def get_job_by_idempotency_key(user_id: str, idempotency_key: str) -> dict | None:
    """Looks the key up inside the existing `request` jsonb column (PostgREST's `->>`
    path operator) rather than adding a dedicated column/migration for it — idempotencyKey
    is already stored there as part of the generation request payload on every job. Scoped
    to user_id so two different users can't collide on the same client-chosen key. No
    unique index backs this (see the caller's note on the narrow race that remains)."""
    if not idempotency_key:
        return None
    rows = await sb_request('GET', 'generation_jobs', params={
        'user_id': f'eq.{user_id}',
        'request->>idempotencyKey': f'eq.{idempotency_key}',
        'select': '*', 'order': 'created_at.desc', 'limit': '1',
    })
    return rows[0] if rows else None

async def create_agent_run(run_id, user_id, goal, status, plan=None, output=None, error=None):
    if not enabled(): return
    await sb_request('POST','agent_runs',{'id':run_id,'user_id':user_id,'goal':goal,'status':status,'plan':plan or [],'output':output,'error':error})

async def update_agent_run(run_id, **fields):
    if not enabled(): return
    fields['updated_at']=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat()
    await sb_request('PATCH','agent_runs',fields,params={'id':f'eq.{run_id}'})

async def get_agent_run(run_id,user_id=None):
    params={'id':f'eq.{run_id}','select':'*','limit':'1'}
    if user_id: params['user_id']=f'eq.{user_id}'
    rows=await sb_request('GET','agent_runs',params=params)
    return rows[0] if rows else None

async def add_feedback(user_id,job_id,rating,accepted,signals):
    rows=await sb_request('POST','brain_feedback',{'user_id':user_id,'generation_job_id':job_id,'rating':rating,'accepted':accepted,'signals':signals})
    return rows[0] if rows else None

async def create_asset(user_id,project_id,kind,storage_key,mime_type=None,bytes_count=None,provenance=None):
    rows=await sb_request('POST','assets',{'user_id':user_id,'project_id':project_id,'kind':kind,'storage_key':storage_key,'mime_type':mime_type,'bytes':bytes_count,'provenance':provenance or {}})
    return rows[0]['id'] if rows else None

# --- Developer Dashboard: admin check + feature flags ------------------------------------
async def is_admin(uid: str) -> bool:
    """Checked via the service-role key (bypasses RLS) — deliberately never checkable from
    a client's own Supabase session, since admin_users has no client-facing RLS policy at
    all (see supabase_schema.sql)."""
    if not uid or not enabled():
        return False
    try:
        rows = await sb_request('GET', 'admin_users', params={'uid': f'eq.{uid}', 'select': 'uid'})
        return bool(rows)
    except PersistenceError:
        return False

async def list_flags() -> list[dict]:
    rows = await sb_request('GET', 'feature_flags', params={'select': '*', 'order': 'key.asc'})
    return rows or []

async def set_flag(key: str, enabled_value: bool, description: str | None) -> dict:
    import time as _time
    now = _time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime())
    patch_body = {'enabled': enabled_value, 'updated_at': now}
    if description is not None:
        patch_body['description'] = description
    # Try updating an existing row first; sb_request's shared headers don't include the
    # `Prefer: resolution=merge-duplicates` a real upsert POST would need, and this
    # PATCH-then-create pattern avoids touching that shared header logic for one caller.
    updated = await sb_request('PATCH', 'feature_flags', patch_body, params={'key': f'eq.{key}'})
    if updated:
        return updated[0]
    created = await sb_request('POST', 'feature_flags', {'key': key, **patch_body})
    return (created or [{}])[0]

# --- Remote Configuration -----------------------------------------------------------------
async def list_remote_config() -> list[dict]:
    rows = await sb_request('GET', 'remote_config', params={'select': '*', 'order': 'key.asc'})
    return rows or []

async def set_remote_config(key: str, value, description: str | None) -> dict:
    import time as _time
    now = _time.strftime('%Y-%m-%dT%H:%M:%SZ', _time.gmtime())
    patch_body = {'value': value, 'updated_at': now}
    if description is not None:
        patch_body['description'] = description
    updated = await sb_request('PATCH', 'remote_config', patch_body, params={'key': f'eq.{key}'})
    if updated:
        return updated[0]
    created = await sb_request('POST', 'remote_config', {'key': key, **patch_body})
    return (created or [{}])[0]

# --- Prompt Management ---------------------------------------------------------------------
async def get_active_prompt(prompt_key: str) -> str | None:
    """Read path used by the gateway itself at call time (e.g. Groq's system prompt). Falls
    back to whatever's hardcoded in server.py if Supabase isn't configured or has no active
    version yet — a missing prompt override must never break generation."""
    if not enabled():
        return None
    try:
        rows = await sb_request('GET', 'system_prompts', params={'prompt_key': f'eq.{prompt_key}', 'is_active': 'eq.true', 'select': 'content', 'limit': 1})
        return rows[0]['content'] if rows else None
    except PersistenceError:
        return None

async def list_prompt_versions(prompt_key: str) -> list[dict]:
    rows = await sb_request('GET', 'system_prompts', params={'prompt_key': f'eq.{prompt_key}', 'select': '*', 'order': 'version.desc'})
    return rows or []

async def create_prompt_version(prompt_key: str, content: str, created_by: str) -> dict:
    existing = await list_prompt_versions(prompt_key)
    next_version = (max((r['version'] for r in existing), default=0)) + 1
    created = await sb_request('POST', 'system_prompts', {
        'prompt_key': prompt_key, 'version': next_version, 'content': content,
        'is_active': False, 'created_by': created_by,
    })
    return (created or [{}])[0]

async def activate_prompt_version(prompt_key: str, version: int) -> dict:
    """Rollback IS this function, run with an older version number — there's no separate
    'rollback' code path, just re-activating a version that already exists in history."""
    # The partial unique index (system_prompts_active_per_key) means only one row per
    # prompt_key can have is_active=true — deactivate the current one first or the insert
    # below the update would violate that constraint.
    await sb_request('PATCH', 'system_prompts', {'is_active': False}, params={'prompt_key': f'eq.{prompt_key}', 'is_active': 'eq.true'})
    updated = await sb_request('PATCH', 'system_prompts', {'is_active': True}, params={'prompt_key': f'eq.{prompt_key}', 'version': f'eq.{version}'})
    if not updated:
        raise PersistenceError(f'No version {version} found for prompt {prompt_key}')
    return updated[0]

# --- Audit Log ------------------------------------------------------------------------------
async def log_admin_action(admin_uid: str, action: str, detail: dict | None = None):
    try:
        await sb_request('POST', 'admin_audit_log', {'admin_uid': admin_uid, 'action': action, 'detail': detail or {}})
    except PersistenceError:
        pass  # Never let audit logging itself block or fail the underlying admin action.

async def list_audit_log(limit: int = 100) -> list[dict]:
    rows = await sb_request('GET', 'admin_audit_log', params={'select': '*', 'order': 'created_at.desc', 'limit': str(min(limit, 500))})
    return rows or []

# --- 2FA -----------------------------------------------------------------------------------
# Brute-force lockout policy for TOTP verification. IP-based rate limiting alone is weak
# against a 6-digit code (1,000,000 combinations) once an attacker can spread attempts
# across multiple source IPs — this makes the failure count travel with the account instead.
TWO_FA_MAX_ATTEMPTS = int(os.getenv('VIDIGEN_2FA_MAX_ATTEMPTS', '5'))
TWO_FA_LOCKOUT_MINUTES = int(os.getenv('VIDIGEN_2FA_LOCKOUT_MINUTES', '15'))

async def get_2fa_secret(uid: str) -> str | None:
    rows = await sb_request('GET', 'admin_2fa', params={'uid': f'eq.{uid}', 'select': 'secret'})
    return rows[0]['secret'] if rows else None

async def enroll_2fa(uid: str, secret: str):
    await sb_request('POST', 'admin_2fa', {'uid': uid, 'secret': secret})

async def has_2fa_enrolled(uid: str) -> bool:
    return await get_2fa_secret(uid) is not None

async def get_2fa_lock_state(uid: str) -> dict:
    """Returns {'locked_until': iso-str-or-None, 'failed_attempts': int}. A row missing
    (not yet enrolled) reads as unlocked with zero attempts — enrollment itself is gated
    separately (an account must already be a verified admin to enroll at all)."""
    rows = await sb_request('GET', 'admin_2fa', params={'uid': f'eq.{uid}', 'select': 'failed_attempts,locked_until'})
    if not rows:
        return {'locked_until': None, 'failed_attempts': 0}
    return {'locked_until': rows[0].get('locked_until'), 'failed_attempts': rows[0].get('failed_attempts', 0)}

async def is_2fa_locked(uid: str) -> tuple[bool, str | None]:
    from datetime import datetime
    state = await get_2fa_lock_state(uid)
    locked_until = state.get('locked_until')
    if not locked_until:
        return False, None
    until = datetime.fromisoformat(str(locked_until).replace('Z', '+00:00'))
    if until > datetime.now(until.tzinfo):
        return True, locked_until
    return False, None  # Lock has expired — caller can proceed; record_2fa_failure/reset will update the row as needed.

async def record_2fa_failure(uid: str):
    """Increments the failure counter and, once TWO_FA_MAX_ATTEMPTS is reached, sets
    locked_until. Read-then-write rather than an atomic RPC — this table is small, checked
    only on the admin-login path (not a hot path), and a lost increment under concurrent
    failed attempts only makes lockout trigger a little later, never a security hole."""
    from datetime import datetime, timedelta, timezone
    state = await get_2fa_lock_state(uid)
    attempts = int(state.get('failed_attempts', 0)) + 1
    patch: dict = {'failed_attempts': attempts}
    if attempts >= TWO_FA_MAX_ATTEMPTS:
        patch['locked_until'] = (datetime.now(timezone.utc) + timedelta(minutes=TWO_FA_LOCKOUT_MINUTES)).isoformat()
    await sb_request('PATCH', 'admin_2fa', patch, params={'uid': f'eq.{uid}'})

async def reset_2fa_failures(uid: str):
    await sb_request('PATCH', 'admin_2fa', {'failed_attempts': 0, 'locked_until': None}, params={'uid': f'eq.{uid}'})

async def create_2fa_session(uid: str, session_token: str, expires_at_iso: str):
    await sb_request('POST', 'admin_2fa_sessions', {'session_token': session_token, 'uid': uid, 'expires_at': expires_at_iso})

async def check_2fa_session(session_token: str, uid: str) -> bool:
    """Also deletes expired sessions it happens to find — light, incidental cleanup rather
    than a separate scheduled job, since this table is small and checked frequently anyway."""
    if not session_token:
        return False
    rows = await sb_request('GET', 'admin_2fa_sessions', params={'session_token': f'eq.{session_token}', 'select': '*'})
    if not rows:
        return False
    row = rows[0]
    if row['uid'] != uid:
        return False
    from gateway.two_factor import is_session_valid
    from datetime import datetime
    expires_at = datetime.fromisoformat(row['expires_at'].replace('Z', '+00:00'))
    if not is_session_valid(expires_at):
        try:
            await sb_request('DELETE', 'admin_2fa_sessions', params={'session_token': f'eq.{session_token}'})
        except PersistenceError:
            pass
        return False
    return True

# --- Supabase user admin (basic slice of "Supabase/R2 Admin") -----------------------------
async def list_supabase_users(page: int = 1, per_page: int = 50) -> dict:
    """Calls Supabase's Auth Admin API directly — a different base path (/auth/v1/admin/)
    than sb_request's /rest/v1/{table} pattern, so it can't reuse that helper as-is."""
    if not enabled():
        return {'users': [], 'total': 0}
    headers = {'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}'}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f'{SUPABASE_URL}/auth/v1/admin/users', headers=headers, params={'page': page, 'per_page': per_page})
    if r.status_code >= 400:
        raise PersistenceError(f'Could not list users ({r.status_code}).')
    data = r.json()
    return {'users': data.get('users', []), 'total': data.get('total') if isinstance(data, dict) else None}

async def get_profile_for_uid(uid: str) -> dict | None:
    rows = await sb_request('GET', 'profiles', params={'id': f'eq.{uid}', 'select': '*'})
    return rows[0] if rows else None

async def delete_supabase_user(uid: str):
    """Deletes the auth.users row; profiles/feedback/generations rows cascade via their
    existing foreign keys (see supabase_schema.sql's `on delete cascade`) — this does NOT
    separately clean up that user's R2 media, which is a real, stated gap (see
    ADMIN_DASHBOARD.md), not silently assumed to be handled."""
    if not enabled():
        raise PersistenceError('Supabase is not configured.')
    headers = {'apikey': SUPABASE_SERVICE_ROLE_KEY, 'Authorization': f'Bearer {SUPABASE_SERVICE_ROLE_KEY}'}
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.delete(f'{SUPABASE_URL}/auth/v1/admin/users/{uid}', headers=headers)
    if r.status_code >= 400:
        raise PersistenceError(f'Could not delete user ({r.status_code}).')

# --- Billing / entitlements / Brain learning ---------------------------------------------
async def ensure_billing_defaults():
    from gateway.billing import DEFAULT_PLANS, DEFAULT_MODEL_COSTS
    if not enabled(): return
    for plan in DEFAULT_PLANS:
        existing=await sb_request('GET','subscription_plans',params={'slug':f"eq.{plan['slug']}",'select':'id'})
        if not existing:
            await sb_request('POST','subscription_plans',plan)
    for cost in DEFAULT_MODEL_COSTS:
        existing=await sb_request('GET','ai_model_prices',params={'model_key':f"eq.{cost['model_key']}",'operation':f"eq.{cost['operation']}",'select':'id'})
        if not existing:
            await sb_request('POST','ai_model_prices',cost)
