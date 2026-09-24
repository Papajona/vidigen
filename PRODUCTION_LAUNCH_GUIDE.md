# Vidigen — full production deployment guide

## The architecture, in one picture

```
Customer's browser / Android app
        │
        ├── static frontend (React/Vite build) ──────► Cloudflare Pages
        │                                                (DNS: vidigen.online)
        │
        └── API calls ────────────────────────────────► Google Cloud Run
                                                           (DNS: api.vidigen.online,
                                                            proxied through Cloudflare)
                                                                │
                              ┌─────────────────────────────────┼───────────────────┐
                              ▼                                 ▼                   ▼
                          Supabase                         Cloudflare R2      Replicate / Groq /
                    (Postgres + Auth + RLS)              (video/image storage)  Paystack / Sentry
```

Nothing here is optional infrastructure you could skip — Supabase holds every user, credit
balance, and job record; R2 holds every generated video/image; Cloud Run is the only piece
that can actually run `ffmpeg`, which is why it can't live on Cloudflare Workers (verified
directly against Cloudflare's own docs earlier in this project — Workers have no subprocess
support at all).

---

## Phase 1 — Supabase (do this first; everything else depends on it existing)

1. Create a project at supabase.com. Pick a region close to your users — this can't be
   changed later without migrating.
2. **SQL Editor** → paste the entire contents of `supabase_schema.sql` from this repo → Run.
   This creates every table: profiles/wallets/credits, billing, feature flags, prompts,
   audit log, 2FA, backups, agent runs, brain learning data — all of it, in one file.
3. **Authentication → Providers**:
   - Enable **Email** (on by default).
   - Enable **Google** — you'll need a Google OAuth client ID/secret from Google Cloud
     Console (APIs & Services → Credentials → OAuth client ID → Web application). Add
     Supabase's callback URL (shown on the provider config page) as an authorized redirect.
4. **Authentication → URL Configuration** → set your production frontend URL
   (`https://vidigen.online`) as the Site URL, and add it to Redirect URLs — Google sign-in
   will fail silently otherwise.
5. **Project Settings → API**, copy three values:
   - Project URL → used as **both** `SUPABASE_URL` and `SUPABASE_JWT_ISSUER` (gateway) and
     `VITE_SUPABASE_URL` (frontend)
   - `anon` public key → `VITE_SUPABASE_ANON_KEY` (frontend only — safe to expose, RLS
     constrains it)
   - `service_role` secret key → `SUPABASE_SERVICE_ROLE_KEY` (**gateway only, never
     frontend, never committed to git**)
6. Make yourself an admin so the dashboard at `/admin` actually works for you:
   ```sql
   insert into admin_users (uid) values ('YOUR-USER-UUID-FROM-AUTH-USERS-TABLE');
   ```
   (Sign up once through the real app first, then find your uuid in
   **Authentication → Users**.)

## Phase 2 — Cloudflare R2 (generated media storage)

1. Cloudflare dashboard → **R2** → create a bucket.
2. **Manage R2 API Tokens** → create one scoped to just that bucket (not full-account
   access) → gives you an access key ID and secret.
3. Turn on public access for the bucket (Settings → Public Access) — either the free
   `*.r2.dev` subdomain for testing, or connect a real custom domain
   (`media.vidigen.online`) for production. This becomes `R2_PUBLIC_BASE_URL`.
4. `R2_ENDPOINT` = `https://<your-cloudflare-account-id>.r2.cloudflarestorage.com`
   (account ID is on the R2 overview page).

## Phase 3 — The other accounts (each needs real signup, I can't create these for you)

- **Replicate**: replicate.com → add billing → Account → API tokens → `REPLICATE_API_TOKEN`.
  Pick a `REPLICATE_MODEL` slug — check replicate.com/collections/text-to-video for what's
  actually current, this list moves fast.
- **Groq** (optional, powers real natural-language AI editing instead of a keyword
  fallback): console.groq.com → API Keys → `GROQ_API_KEY`.
- **Paystack**: dashboard.paystack.com → Settings → API Keys & Webhooks. You'll have both
  test and live keys — **use the test key until you've verified checkout end-to-end**, then
  switch. Set the webhook URL to `https://api.vidigen.online/api/billing/webhook/paystack`.
- **Sentry** (optional but recommended before public launch): sentry.io → new project →
  copy the DSN → `SENTRY_DSN` (gateway) and `VITE_SENTRY_DSN` (frontend, separate DSN or
  same project, your call).

## Phase 4 — Google Cloud Run (the gateway)

Using the `Dockerfile` and `.github/workflows/deploy-gateway.yml` already in this repo:

1. One-time GCP setup:
   ```bash
   gcloud services enable run.googleapis.com artifactregistry.googleapis.com
   gcloud artifacts repositories create vidigen --repository-format=docker --location=us-central1
   ```
2. Set up Workload Identity Federation so GitHub Actions can deploy without a downloaded
   service-account key file (a real, common leak vector avoided entirely this way) — full
   steps in `DEPLOY_GCP.md` in this repo.
3. Add these as **GitHub repo secrets**: `GCP_PROJECT_ID`, `GCP_WORKLOAD_IDENTITY_PROVIDER`,
   `GCP_SERVICE_ACCOUNT`.
4. Set every gateway env var from `.env.example` on the Cloud Run service itself — use
   **Secret Manager** for anything sensitive (`SUPABASE_SERVICE_ROLE_KEY`,
   `REPLICATE_API_TOKEN`, `PAYSTACK_SECRET_KEY`, `SENTRY_DSN`), not plain env vars, and
   never put secrets in the GitHub Actions YAML itself.
5. Push to `main` — the workflow builds the Docker image and deploys automatically.
6. **Before this is live for real customers**, flip these two from their defaults:
   - `VIDIGEN_PAYMENT_TEST_MODE=false` (defaults to `true` in `.env.example` — deploying
     with this still on means no real payment is ever actually processed)
   - `PAYSTACK_SECRET_KEY` → your **live** key, not the test one
7. In Cloudflare DNS, add a CNAME/A record for `api.vidigen.online` pointing at the Cloud Run
   service's URL, proxied (orange cloud) through Cloudflare for DDoS protection/caching.

I verified two real constraints on Cloud Run earlier in this project, worth repeating here
since they matter at this exact step: request timeout caps at 60 minutes (set via
`--timeout=3600` in the deploy command, already in the workflow), and the Live Developer
Logs dashboard feature only works correctly with a single instance
(`--max-instances=1` — also already set) until it moves to a shared backing store.

## Phase 5 — Cloudflare Pages (the frontend)

1. Cloudflare dashboard → **Workers & Pages → Create → Pages → Connect to Git** → select
   this GitHub repo.
2. Build settings: build command `npm run build`, output directory `dist`.
3. **Environment variables** (Pages project settings, not the gateway's):
   `VITE_VIDIGEN_GATEWAY_URL=https://api.vidigen.online`, `VITE_SUPABASE_URL`,
   `VITE_SUPABASE_ANON_KEY`, and the Google Pay/Sentry `VITE_*` vars from
   `.env.example` as needed.
4. Add your custom domain (`vidigen.online`) under the Pages project's **Custom domains** tab
   — since the domain's already on Cloudflare, this is a few clicks, not a DNS migration.
5. Every push to `main` now auto-deploys the frontend. No separate GitHub Action needed for
   this half — Cloudflare Pages' GitHub integration handles it directly.

## Phase 6 — The two CORS gotchas that will otherwise cost you an afternoon

Both already flagged earlier in this project, repeating because they bite silently:
1. Add your real production frontend origin to `VIDIGEN_ALLOWED_ORIGINS` on the gateway —
   `https://vidigen.online`, not just the localhost defaults in `.env.example`.
2. If you also ship the Android app, add `https://localhost` too (Capacitor's default
   WebView origin) — otherwise the compiled app's API calls get silently blocked by CORS.

## Phase 7 — Before you actually announce this publicly

- [ ] Confirmed `VIDIGEN_PAYMENT_TEST_MODE=false` and a **live** Paystack key
- [ ] Sent yourself a real test payment and confirmed a webhook arrived and credits landed
- [ ] Confirmed 2FA works for your own admin account (`VIDIGEN_ADMIN_2FA_REQUIRED=true` by
      default — good, leave it)
- [ ] Ran the Automated Testing suite from the `/admin` dashboard against production once
- [ ] Confirmed Sentry actually receives a test error (trigger one deliberately, check the
      Sentry dashboard)
- [ ] Confirmed output moderation enforcement on a real provider output. The gateway now checks
      completed image/video outputs before delivery when moderation enforcement is enabled.
- [ ] Decided on Backup & Rollback's actual retention/testing cadence — it's built, but a
      backup you've never test-restored isn't a backup you can trust yet.

## What still requires real-account verification before public launch

I have never had a real Supabase project, Google Cloud account, or Cloudflare zone
connected to my sandbox — everything above is correct as far as I can verify by reading the
actual code and cross-checking current provider documentation, the same standard I've held
throughout this project for the Kotlin plugin and the Dockerfile. The first real run-through
of this full sequence, on your actual accounts, is the real test.
