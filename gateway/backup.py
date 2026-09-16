"""Backup & Rollback for Vidigen's Supabase application data.

Honest scope, stated up front because this is exactly the kind of feature where implying
more safety than exists is dangerous: this is a LOGICAL SNAPSHOT of table contents via
Supabase's REST API, exported as one JSON file to R2 — not a binary pg_dump, not a
point-in-time database restore, and not transactionally consistent across tables (each
table is read independently; a write happening mid-backup could appear in one table's
snapshot but not a related one). Supabase's managed architecture doesn't expose direct
Postgres-level backup tooling through the REST API, so a full pg_dump-equivalent isn't
available from this gateway at all — this is the honest, buildable version of "backup",
not a compromise on the way to a better one that was skipped.

Restore is "best effort": it re-inserts snapshot rows into their tables via upsert
(on_conflict on primary key), in an order chosen to reduce (not guarantee) foreign-key
ordering problems. It does NOT delete rows created after the snapshot, does NOT handle
schema changes between backup and restore, and does NOT roll back credit/payment side
effects that happened after the snapshot (a restored credit_wallets row could show a
balance a user has since legitimately spent below or above). This is a recovery aid for
catastrophic data loss or admin mistakes, not an undo button for financial transactions.
"""
from __future__ import annotations
import json, os
from datetime import datetime, timezone
import httpx
from fastapi import APIRouter, Depends, HTTPException
from gateway import persistence

# Order matters for restore: tables with no foreign keys into other backed-up tables go
# first, so a restore is less likely to hit a dangling reference — but since every table
# here references only auth.users (not each other), the real ordering risk is small. Still
# sequenced deliberately rather than left to dict iteration order.
BACKUP_TABLES = [
    'subscription_plans', 'ai_model_prices', 'feature_flags', 'remote_config', 'system_prompts',
    'admin_users', 'projects', 'assets', 'generation_jobs',
    'credit_wallets', 'credit_transactions', 'subscriptions', 'payments', 'payment_events',
    'brain_patterns', 'brain_output_samples', 'brain_feedback',
]

PRIMARY_KEY_BY_TABLE = {
    'subscription_plans': 'slug', 'ai_model_prices': 'id', 'feature_flags': 'key',
    'remote_config': 'key', 'system_prompts': 'id', 'admin_users': 'uid',
    'projects': 'id', 'assets': 'id', 'generation_jobs': 'id',
    'credit_wallets': 'user_id', 'credit_transactions': 'id', 'subscriptions': 'id',
    'payments': 'id', 'payment_events': 'id', 'brain_patterns': 'id',
    'brain_output_samples': 'id', 'brain_feedback': 'id',
}


def _r2_client():
    import boto3
    from botocore.config import Config as BotoConfig
    endpoint, access_key, secret_key, bucket = (os.getenv(k, '') for k in
        ('R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET'))
    if not all([endpoint, access_key, secret_key, bucket]):
        raise HTTPException(501, 'Cloudflare R2 is not fully configured on this gateway — required for backups.')
    client = boto3.client('s3', endpoint_url=endpoint, aws_access_key_id=access_key,
                           aws_secret_access_key=secret_key, config=BotoConfig(signature_version='s3v4'), region_name='auto')
    return client, bucket


async def create_backup(admin_uid: str) -> dict:
    if not persistence.enabled():
        raise HTTPException(503, 'Supabase persistence must be configured to create a backup.')
    snapshot = {}
    table_counts = {}
    for table in BACKUP_TABLES:
        try:
            rows = await persistence.sb_request('GET', table, params={'select': '*'})
        except persistence.PersistenceError as e:
            # A single table failing to export shouldn't silently produce an incomplete
            # backup that looks complete — fail the whole backup loudly instead.
            raise HTTPException(502, f'Backup failed exporting table "{table}": {e}')
        snapshot[table] = rows or []
        table_counts[table] = len(rows or [])

    body = json.dumps({
        'created_at': datetime.now(timezone.utc).isoformat(),
        'tables': snapshot,
    }).encode('utf-8')

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    r2_key = f'backups/vidigen-backup-{timestamp}.json'
    client, bucket = _r2_client()
    try:
        client.put_object(Bucket=bucket, Key=r2_key, Body=body, ContentType='application/json')
    except Exception as e:
        raise HTTPException(502, f'Could not upload backup to R2: {e}')

    record = await persistence.sb_request('POST', 'backups', {
        'r2_key': r2_key, 'created_by': admin_uid, 'table_counts': table_counts,
        'size_bytes': len(body), 'status': 'complete',
    })
    await persistence.log_admin_action(admin_uid, 'create_backup', {'r2_key': r2_key, 'table_counts': table_counts})
    return record[0] if record else {'r2_key': r2_key, 'table_counts': table_counts}


async def list_backups() -> list[dict]:
    if not persistence.enabled():
        return []
    return await persistence.sb_request('GET', 'backups', params={'select': '*', 'order': 'created_at.desc', 'limit': '50'}) or []


async def prune_old_backups(keep: int = 7, admin_uid: str | None = None) -> dict:
    backups = await list_backups()
    to_delete = backups[keep:]
    if not to_delete:
        return {'deleted': 0}
    client, bucket = _r2_client()
    deleted = 0
    for b in to_delete:
        try:
            client.delete_object(Bucket=bucket, Key=b['r2_key'])
            await persistence.sb_request('DELETE', 'backups', params={'id': f"eq.{b['id']}"})
            deleted += 1
        except Exception:
            continue  # Best-effort cleanup — one failed deletion shouldn't stop the rest.
    if admin_uid:
        await persistence.log_admin_action(admin_uid, 'prune_backups', {'deleted': deleted, 'kept': keep})
    return {'deleted': deleted}


async def restore_backup(backup_id: str, admin_uid: str) -> dict:
    if not persistence.enabled():
        raise HTTPException(503, 'Supabase persistence must be configured to restore a backup.')
    rows = await persistence.sb_request('GET', 'backups', params={'id': f'eq.{backup_id}', 'select': '*', 'limit': '1'})
    if not rows:
        raise HTTPException(404, 'Backup not found.')
    backup_meta = rows[0]
    client, bucket = _r2_client()
    try:
        obj = client.get_object(Bucket=bucket, Key=backup_meta['r2_key'])
        snapshot = json.loads(obj['Body'].read())
    except Exception as e:
        raise HTTPException(502, f'Could not read backup from R2: {e}')

    results = {}
    for table in BACKUP_TABLES:
        table_rows = (snapshot.get('tables') or {}).get(table, [])
        if not table_rows:
            results[table] = {'restored': 0}
            continue
        pk = PRIMARY_KEY_BY_TABLE.get(table, 'id')
        restored = 0
        errors = 0
        for row in table_rows:
            try:
                # Upsert via PATCH-then-POST, same pattern as feature_flags/remote_config
                # elsewhere in this codebase — this Supabase REST setup has no configured
                # `Prefer: resolution=merge-duplicates` upsert path, so this mirrors what
                # already works rather than introducing a second upsert convention.
                key_val = row.get(pk)
                if key_val is None:
                    errors += 1; continue
                updated = await persistence.sb_request('PATCH', table, row, params={pk: f'eq.{key_val}'})
                if not updated:
                    await persistence.sb_request('POST', table, row)
                restored += 1
            except persistence.PersistenceError:
                errors += 1
        results[table] = {'restored': restored, 'errors': errors}

    await persistence.log_admin_action(admin_uid, 'restore_backup', {'backup_id': backup_id, 'results': results})
    return {'restored_from': backup_meta['r2_key'], 'created_at': backup_meta['created_at'], 'results': results}


def build_router(admin_dependency):
    router = APIRouter(prefix='/api/admin/backup', tags=['backup'])

    @router.get('')
    async def list_route(user=Depends(admin_dependency)):
        return {'backups': await list_backups()}

    @router.post('')
    async def create_route(user=Depends(admin_dependency)):
        return await create_backup(user.get('sub'))

    @router.post('/{backup_id}/restore')
    async def restore_route(backup_id: str, user=Depends(admin_dependency)):
        return await restore_backup(backup_id, user.get('sub'))

    @router.post('/prune')
    async def prune_route(keep: int = 7, user=Depends(admin_dependency)):
        if keep < 1:
            raise HTTPException(400, 'keep must be at least 1.')
        return await prune_old_backups(keep, user.get('sub'))

    return router
