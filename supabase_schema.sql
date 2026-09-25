-- Vidigen production data model: Supabase/Postgres stores metadata, not large media blobs.
create extension if not exists pgcrypto;

create table if not exists public.projects (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  name text not null check (char_length(name) between 1 and 160), timeline jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists public.assets (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  project_id uuid references public.projects(id) on delete cascade, kind text not null check (kind in ('video','image','audio','caption','export')),
  storage_key text not null, mime_type text, bytes bigint check (bytes is null or bytes >= 0), provenance jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create table if not exists public.generation_jobs (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  project_id uuid references public.projects(id) on delete cascade, provider text not null, model text not null,
  request jsonb not null default '{}'::jsonb, idempotency_key text,
  status text not null default 'queued' check (status in ('queued','processing','completed','failed','cancelled')),
  output_asset_id uuid references public.assets(id), error text, created_at timestamptz not null default now(), completed_at timestamptz
);
create unique index if not exists generation_jobs_user_idempotency_key_idx
  on public.generation_jobs(user_id, idempotency_key) where idempotency_key is not null;
create table if not exists public.brain_feedback (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  generation_job_id uuid references public.generation_jobs(id) on delete set null, rating smallint check (rating between 0 and 5),
  signals jsonb not null default '{}'::jsonb, accepted boolean, created_at timestamptz not null default now()
);
create table if not exists public.brain_datasets (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  version text not null, source_count integer not null default 0, provenance jsonb not null default '{}'::jsonb,
  license_summary text, status text not null default 'draft' check (status in ('draft','validated','training','evaluating','champion','rejected')),
  created_at timestamptz not null default now()
);

alter table public.projects enable row level security; alter table public.assets enable row level security;
alter table public.generation_jobs enable row level security; alter table public.brain_feedback enable row level security; alter table public.brain_datasets enable row level security;
create policy "own projects" on public.projects for all using (auth.uid()=user_id) with check (auth.uid()=user_id);
create policy "own assets" on public.assets for all using (auth.uid()=user_id) with check (auth.uid()=user_id);
create policy "own jobs" on public.generation_jobs for all using (auth.uid()=user_id) with check (auth.uid()=user_id);
create policy "own feedback" on public.brain_feedback for all using (auth.uid()=user_id) with check (auth.uid()=user_id);
create policy "own datasets" on public.brain_datasets for all using (auth.uid()=user_id) with check (auth.uid()=user_id);

-- Production hardening: validate ownership and basic payload shape server-side as a second line of defense.
create index if not exists generation_jobs_user_created_idx on public.generation_jobs(user_id, created_at desc);
create index if not exists brain_feedback_user_created_idx on public.brain_feedback(user_id, created_at desc);
create index if not exists assets_user_created_idx on public.assets(user_id, created_at desc);

-- Developer Dashboard foundation (admin-only; see ADMIN_DASHBOARD.md for scope).
-- Note on brain_datasets above: that table's lifecycle (draft/training/evaluating/champion)
-- implies a real model-training pipeline. Nothing in gateway/ or src/ references it — it's
-- unbuilt scaffolding, not a working feature. Left in place rather than deleted, since it
-- may be intentional groundwork for later, but flagging it plainly here so it isn't mistaken
-- for an existing capability.

create table if not exists public.admin_users (
  uid uuid primary key references auth.users (id) on delete cascade,
  added_at timestamptz not null default now(),
  note text
);
alter table public.admin_users enable row level security;
-- Deliberately NO client-facing policies at all — only the gateway's service-role key
-- (which bypasses RLS) can read this table. A regular user's own client should never be
-- able to even check who else is an admin.

create table if not exists public.feature_flags (
  key text primary key,
  enabled boolean not null default false,
  description text,
  updated_at timestamptz not null default now()
);
alter table public.feature_flags enable row level security;

create policy "Any signed-in user can read flags"
  on public.feature_flags for select
  using (auth.uid() is not null);
-- No insert/update/delete policy for ordinary users -> writes only ever happen through the
-- gateway's service-role key, gated by its own admin check (see require_admin in server.py).

-- Remote Configuration: same read/write model as feature_flags, but for non-boolean values
-- (numeric limits, string defaults/endpoints) rather than duplicating that mechanism.
create table if not exists public.remote_config (
  key text primary key,
  value jsonb not null,
  description text,
  updated_at timestamptz not null default now()
);
alter table public.remote_config enable row level security;
create policy "Any signed-in user can read remote config"
  on public.remote_config for select
  using (auth.uid() is not null);
-- No client write policy, same reasoning as feature_flags.

-- Prompt Management: versioned system prompts, with the ability to roll back by simply
-- reactivating an older version rather than deleting history.
create table if not exists public.system_prompts (
  id bigint generated always as identity primary key,
  prompt_key text not null,          -- e.g. 'groq_edit_planner'
  version int not null,
  content text not null,
  is_active boolean not null default false,
  created_at timestamptz not null default now(),
  created_by uuid references auth.users (id)
);
alter table public.system_prompts enable row level security;
-- No client-facing policies at all — prompts are read by the gateway itself via the
-- service-role key at call time, not fetched by the app. Only /api/admin/* can see or
-- change these.
create unique index if not exists system_prompts_active_per_key
  on public.system_prompts (prompt_key) where is_active;

-- Audit Log: append-only record of every admin action, for the Security Center item.
create table if not exists public.admin_audit_log (
  id bigint generated always as identity primary key,
  admin_uid uuid not null references auth.users (id),
  action text not null,
  detail jsonb,
  created_at timestamptz not null default now()
);
alter table public.admin_audit_log enable row level security;
-- No client-facing policies — written only by the gateway (service-role key) after a
-- successful admin action, read only through /api/admin/audit-log.

-- Two-factor authentication for admin accounts (Security Center). TOTP via pyotp — an
-- established, audited library, not hand-rolled crypto.
create table if not exists public.admin_2fa (
  uid uuid primary key references auth.users (id) on delete cascade,
  secret text not null,          -- base32 TOTP secret; only ever read/written server-side
  enrolled_at timestamptz not null default now(),
  -- Account-level brute-force lockout, additive to the gateway's IP-based rate limiter.
  -- IP-based limiting alone is weak against a 6-digit TOTP code (1,000,000 combinations)
  -- once an attacker can spread guesses across multiple source IPs; this makes the account
  -- itself the thing that locks, regardless of how many IPs are used against it.
  failed_attempts integer not null default 0,
  locked_until timestamptz
);
-- Idempotent migration for deployments that already created admin_2fa before these two
-- columns existed — `create table if not exists` above is a no-op on an existing table, so
-- these ADD COLUMNs are what actually lands the lockout fields on an already-deployed DB.
alter table public.admin_2fa add column if not exists failed_attempts integer not null default 0;
alter table public.admin_2fa add column if not exists locked_until timestamptz;
alter table public.admin_2fa enable row level security;
-- No client-facing policies — an admin never reads their own raw TOTP secret back over
-- the API after enrollment; the QR/manual-entry code is shown once, at enrollment time,
-- directly in the enrollment response, not fetched again later.

-- Short-lived session issued after a successful TOTP check, required alongside the
-- Supabase JWT for admin actions once 2FA is enrolled. This is what makes 2FA actually
-- mean something — without a session concept, you'd have to resubmit a TOTP code on every
-- single admin API call, which no real 2FA system does.
create table if not exists public.admin_2fa_sessions (
  session_token text primary key,
  uid uuid not null references auth.users (id) on delete cascade,
  created_at timestamptz not null default now(),
  expires_at timestamptz not null
);
alter table public.admin_2fa_sessions enable row level security;
-- No client-facing policies — issued and checked only by the gateway.

-- AI Agent orchestration audit/state. The gateway may use local in-memory state when
-- Supabase is unavailable, but production deployments should apply this table.
create table if not exists public.agent_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  goal text not null,
  status text not null check (status in ('queued','running','completed','failed','cancelled')),
  plan jsonb not null default '[]'::jsonb,
  output jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
alter table public.agent_runs enable row level security;
create policy "own agent runs" on public.agent_runs for all using (auth.uid()=user_id) with check (auth.uid()=user_id);
create index if not exists agent_runs_user_created_idx on public.agent_runs(user_id, created_at desc);

-- Billing / subscriptions / credits -------------------------------------------------------
create table if not exists public.subscription_plans (
  slug text primary key, id uuid unique not null default gen_random_uuid(),
  name text not null, price_ghs numeric(12,2) not null default 0 check (price_ghs >= 0),
  monthly_credits integer not null default 0 check (monthly_credits >= 0),
  storage_gb numeric not null default 0 check (storage_gb >= 0), watermark boolean not null default true,
  commercial_use boolean not null default false, priority boolean not null default false,
  interval text not null default 'monthly' check (interval in ('monthly','annually')),
  paystack_plan_code text, active boolean not null default true,
  created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create table if not exists public.subscriptions (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  plan_id uuid references public.subscription_plans(id), plan_slug text not null references public.subscription_plans(slug), status text not null default 'active'
    check(status in ('trialing','active','past_due','cancelled','expired','suspended')),
  started_at timestamptz not null default now(), current_period_start timestamptz,
  current_period_end timestamptz, cancel_at_period_end boolean not null default false,
  paystack_customer_code text, paystack_subscription_code text, paystack_transaction_reference text,
  updated_at timestamptz not null default now()
);
create unique index if not exists active_subscription_per_user on public.subscriptions(user_id) where status='active';
create table if not exists public.payments (
  id uuid primary key default gen_random_uuid(), user_id uuid references auth.users(id) on delete cascade,
  reference text unique, provider text, payment_method text, plan_slug text,
  amount_ghs numeric(12,2), amount numeric(12,2), currency text not null default 'GHS',
  status text not null default 'pending', metadata jsonb not null default '{}'::jsonb,
  verified_at timestamptz, created_at timestamptz not null default now(), updated_at timestamptz not null default now(),
  plan_id uuid references public.subscription_plans(id)
);
create table if not exists public.payment_events (
  id uuid primary key default gen_random_uuid(), provider text not null, provider_event_id text not null unique,
  event text not null, payload jsonb not null default '{}'::jsonb, created_at timestamptz not null default now()
);
create table if not exists public.credit_wallets (
  user_id uuid primary key references auth.users(id) on delete cascade, balance integer not null default 0 check(balance >= 0),
  monthly_allowance integer not null default 0, updated_at timestamptz not null default now()
);
create table if not exists public.credit_transactions (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  amount integer not null, kind text not null, operation text, model_key text, metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create table if not exists public.ai_model_prices (
  id uuid primary key default gen_random_uuid(), model_key text not null, operation text not null,
  credits_per_5s integer not null check(credits_per_5s > 0), updated_at timestamptz not null default now(),
  unique(model_key, operation)
);

alter table public.subscription_plans enable row level security;
alter table public.subscriptions enable row level security;
alter table public.payments enable row level security;
alter table public.payment_events enable row level security;
alter table public.credit_wallets enable row level security;
alter table public.credit_transactions enable row level security;
alter table public.ai_model_prices enable row level security;
create policy "signed users read active subscription plans" on public.subscription_plans for select using (auth.uid() is not null and active=true);
create policy "own subscriptions" on public.subscriptions for select using (auth.uid()=user_id);
create policy "own payments" on public.payments for select using (auth.uid()=user_id);
create policy "own wallet" on public.credit_wallets for select using (auth.uid()=user_id);
create policy "own credit transactions" on public.credit_transactions for select using (auth.uid()=user_id);
create policy "signed users read model prices" on public.ai_model_prices for select using (auth.uid() is not null);
create index if not exists subscriptions_user_idx on public.subscriptions(user_id, updated_at desc);
create index if not exists payments_user_created_idx on public.payments(user_id, created_at desc);
create index if not exists credit_transactions_user_created_idx on public.credit_transactions(user_id, created_at desc);

-- Output learning: sample metadata and reusable creative patterns. This is retrieval/adaptation,
-- not automatic foundation-model retraining.
create table if not exists public.brain_output_samples (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  output_url text not null, media_type text not null check(media_type in ('video','image')), analysis jsonb not null default '{}'::jsonb,
  rating smallint not null default 0 check(rating between 0 and 5), accepted boolean not null default false,
  source_prompt text, tags text[] not null default '{}', created_at timestamptz not null default now()
);
create table if not exists public.brain_patterns (
  id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
  pattern_type text not null, pattern jsonb not null default '{}'::jsonb,
  source_sample_id uuid references public.brain_output_samples(id) on delete set null, created_at timestamptz not null default now()
);
alter table public.brain_output_samples enable row level security;
alter table public.brain_patterns enable row level security;
create policy "own brain output samples" on public.brain_output_samples for all using (auth.uid()=user_id) with check (auth.uid()=user_id);
create policy "own brain patterns" on public.brain_patterns for all using (auth.uid()=user_id) with check (auth.uid()=user_id);
create index if not exists brain_output_user_created_idx on public.brain_output_samples(user_id, created_at desc);
create index if not exists brain_pattern_user_created_idx on public.brain_patterns(user_id, created_at desc);

-- Seed the internal credit weights. These are application credit weights, not provider prices.
insert into public.subscription_plans (slug,name,price_ghs,monthly_credits,storage_gb,watermark,commercial_use,priority,interval)
values
('free','Free',0,0,0.5,true,false,false,'monthly'),
('creator','Creator',149,600,25,false,true,false,'monthly'),
('pro','Pro',399,1800,100,false,true,true,'monthly'),
('studio','Studio',999,5000,500,false,true,true,'monthly')
on conflict (slug) do nothing;
insert into public.ai_model_prices (model_key,operation,credits_per_5s)
values ('default-video','video',80),('bytedance/seedance-2.0','video',110),('runway-premium','video',120),('default-image','image',6)
on conflict (model_key,operation) do nothing;

-- Atomic credit balance operations. billing.py originally did GET balance -> compute in
-- Python -> PATCH new balance, which is a real lost-update race: two concurrent requests
-- (two simultaneous generations, or a webhook retry racing a live request) can both read
-- the same starting balance, each compute their own "new" balance, and the second write
-- silently overwrites the first — one of the two charges/credits is lost. The existing
-- `balance >= 0` CHECK constraint does NOT catch this: both individually-computed values
-- can each satisfy `>= 0` while the combined effect is wrong. Fixing this requires the
-- balance change to happen in a single atomic SQL statement, not two round-trips.
create or replace function public.consume_credits_atomic(p_user_id uuid, p_amount integer)
returns table(new_balance integer)
language plpgsql
security definer
set search_path = public
as $$
begin
  return query
    update public.credit_wallets
    set balance = balance - p_amount, updated_at = now()
    where user_id = p_user_id and balance >= p_amount
    returning balance;
end;
$$;

create or replace function public.refund_credits_atomic(p_user_id uuid, p_amount integer)
returns table(new_balance integer)
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.credit_wallets (user_id, balance, monthly_allowance)
  values (p_user_id, p_amount, 0)
  on conflict (user_id) do update
    set balance = public.credit_wallets.balance + excluded.balance, updated_at = now();
  return query select balance as new_balance from public.credit_wallets where user_id = p_user_id;
end;
$$;

create or replace function public.grant_subscription_credits_atomic(p_user_id uuid, p_amount integer, p_monthly_allowance integer)
returns table(new_balance integer)
language plpgsql
security definer
set search_path = public
as $$
begin
  insert into public.credit_wallets (user_id, balance, monthly_allowance)
  values (p_user_id, p_amount, p_monthly_allowance)
  on conflict (user_id) do update
    set balance = public.credit_wallets.balance + excluded.balance,
        monthly_allowance = excluded.monthly_allowance,
        updated_at = now();
  return query select balance as new_balance from public.credit_wallets where user_id = p_user_id;
end;
$$;


revoke all on function public.consume_credits_atomic(uuid,integer) from public;
revoke all on function public.consume_credits_atomic(uuid,integer) from anon, authenticated;
revoke all on function public.refund_credits_atomic(uuid,integer) from public;
revoke all on function public.refund_credits_atomic(uuid,integer) from anon, authenticated;
revoke all on function public.grant_subscription_credits_atomic(uuid,integer,integer) from public;
revoke all on function public.grant_subscription_credits_atomic(uuid,integer,integer) from anon, authenticated;
grant execute on function public.consume_credits_atomic(uuid,integer) to service_role;
grant execute on function public.refund_credits_atomic(uuid,integer) to service_role;
grant execute on function public.grant_subscription_credits_atomic(uuid,integer,integer) to service_role;

-- Free-plan daily entitlements: five avatar generations and five photo enhancements per UTC day.
create table if not exists public.daily_feature_usage (
  user_id uuid not null references auth.users(id) on delete cascade,
  usage_date date not null default current_date,
  feature text not null check (feature in ('avatar','photo_enhance')),
  count integer not null default 0 check (count >= 0),
  updated_at timestamptz not null default now(),
  primary key (user_id, usage_date, feature)
);
alter table public.daily_feature_usage enable row level security;
create policy "own daily feature usage" on public.daily_feature_usage for select using (auth.uid()=user_id);
create index if not exists daily_feature_usage_user_date_idx on public.daily_feature_usage(user_id, usage_date desc);

create or replace function public.consume_daily_feature_atomic(p_user_id uuid, p_feature text, p_limit integer)
returns table(new_count integer, allowed boolean)
language plpgsql
security definer
set search_path=public
as $
declare
  current_count integer;
begin
  if p_limit < 1 then raise exception 'daily limit must be positive'; end if;

  insert into public.daily_feature_usage(user_id,usage_date,feature,count)
  values(p_user_id,current_date,p_feature,1)
  on conflict(user_id,usage_date,feature) do update
    set count=public.daily_feature_usage.count+1, updated_at=now()
    where public.daily_feature_usage.count < p_limit
  returning count into current_count;

  -- When the row is already at the limit, PostgreSQL's conditional
  -- ON CONFLICT update affects zero rows. The previous implementation then
  -- selected count <= p_limit, incorrectly allowing one extra request.
  if not found then
    select count into current_count
      from public.daily_feature_usage
     where user_id=p_user_id and usage_date=current_date and feature=p_feature;
    return query select current_count, false;
    return;
  end if;

  return query select current_count, true;
end;
$;

create or replace function public.release_daily_feature_atomic(p_user_id uuid, p_feature text)
returns table(released boolean)
language plpgsql
security definer
set search_path=public
as $
begin
  update public.daily_feature_usage
     set count=greatest(count-1,0), updated_at=now()
   where user_id=p_user_id and usage_date=current_date and feature=p_feature;
  return query select true;
end;
$;

revoke all on function public.consume_daily_feature_atomic(uuid,text,integer) from public,anon,authenticated;
revoke all on function public.release_daily_feature_atomic(uuid,text) from public,anon,authenticated;
grant execute on function public.consume_daily_feature_atomic(uuid,text,integer) to service_role;
grant execute on function public.release_daily_feature_atomic(uuid,text) to service_role;

-- Backup & Rollback metadata. The actual snapshot data lives in R2 (as one JSON file per
-- backup) — this table just tracks what exists and its provenance.
create table if not exists public.backups (
  id uuid primary key default gen_random_uuid(),
  r2_key text not null,
  created_at timestamptz not null default now(),
  created_by uuid references auth.users(id),
  table_counts jsonb not null default '{}'::jsonb,
  size_bytes bigint,
  status text not null default 'complete' check (status in ('complete','failed'))
);
alter table public.backups enable row level security;
-- No client-facing policies — admin-only, via the service-role key, same as the other
-- admin-only tables in this file.
