# Deploying the gateway to Google Cloud Run

## Why Cloud Run, not Cloudflare, for the gateway
Verified directly: Cloudflare Workers cannot run subprocesses at all — no `ffmpeg`, no
`ffprobe`. This gateway uses both for captions, auto-reframe, rendering, and the admin test
runner. Cloud Run runs a real container (this repo's `Dockerfile` installs actual `ffmpeg`
via `apt-get`), so none of that is a problem here. Keep Cloudflare for the domain/DNS/CDN in
front of both the frontend (Cloudflare Pages) and this gateway (a subdomain like
`api.yourdomain.com` pointed at Cloud Run) — that split is normal, not a compromise.

## Two real caveats before you deploy — not hypothetical, verified against current Cloud Run docs

1. **Live Developer Logs won't work correctly across multiple instances.** The log
   broadcaster (`_log_ring`, `_log_subscribers` in `server.py`) is in-memory, per-instance.
   Cloud Run's own docs are direct about this: *"the most difficult part of creating
   [long-lived streaming] services on Cloud Run is synchronizing data between multiple Cloud
   Run instances."* If Cloud Run scales to 2+ instances, an admin's SSE connection lands on
   one instance and only sees that instance's logs — not a global view. The GitHub Actions
   workflow above sets `--max-instances=1` specifically to sidestep this for now. That caps
   your gateway's ability to scale under load, which is the right tradeoff until this
   feature moves to a shared backing store (e.g., Supabase Realtime or a Redis pub/sub) —
   raising `max-instances` before then will make this feature silently unreliable, not fail
   loudly.
2. **The Agent system has the same underlying issue, and it's customer-facing.** `RUNS`
   in `gateway/agent/api.py` is also an in-memory, per-instance dict. Found and partially
   fixed during a later review: `GET /api/agent/runs/{run_id}` now falls back to the
   Supabase-persisted copy when the in-memory cache misses, so status polling works
   correctly regardless of which instance handles the request. `POST /api/agent/runs/{id}/
   cancel` does NOT have an equivalent fix — cancelling requires reaching the actual
   `asyncio.Task` executing the run, which only exists on the instance that started it. A
   full fix needs a message queue routing cancel requests to the right instance; until then,
   a cancel request that lands on the wrong instance correctly 404s rather than silently
   pretending to cancel a run it can't actually stop. This is a second, independent reason
   `--max-instances=1` matters right now — not just the logging feature.
3. **Request timeout is capped at 60 minutes, and it applies to the SSE stream too.** Cloud
   Run's own docs: default 5 minutes, configurable up to 3600 seconds max — no higher. The
   deploy command above sets `--timeout=3600`. The dashboard's `EventSource` will
   auto-reconnect when that's hit (browsers do this natively for SSE), so this degrades
   gracefully rather than breaking, but it's worth knowing rather than being surprised by it.

## What I could NOT verify myself
I don't have Docker in this environment, and my sandbox's network only reaches
npm/PyPI/GitHub — not Docker Hub. So the `Dockerfile` in this repo has never actually been
built here. It's written correctly as far as I can verify by reading it, but "reads
correctly" and "builds successfully" are different claims, the same distinction I've held to
throughout this project for the Kotlin plugin. Run `docker build -t test .` yourself before
wiring up the GitHub Action, so the first real build attempt isn't inside CI.

## One-time GCP setup (before the GitHub Action can deploy anything)
```bash
gcloud services enable run.googleapis.com artifactregistry.googleapis.com
gcloud artifacts repositories create vidigen --repository-format=docker --location=us-central1

# Workload Identity Federation — avoids ever downloading a service-account key file
gcloud iam workload-identity-pools create github-pool --location=global
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github-pool \
  --issuer-uri=https://token.actions.githubusercontent.com \
  --attribute-mapping="google.subject=assertion.sub"
```
Then add `GCP_PROJECT_ID`, `GCP_WORKLOAD_IDENTITY_PROVIDER`, and `GCP_SERVICE_ACCOUNT` as
GitHub repo secrets. Full IAM binding steps are in Google's own Workload Identity Federation
docs — worth reading directly rather than me summarizing a security-sensitive setup from
memory.

## Environment variables
Every var in `.env.example` needs to be set on the Cloud Run service itself (`--set-env-vars`
or, better, Secret Manager for anything sensitive — `SUPABASE_SERVICE_ROLE_KEY`,
`REPLICATE_API_TOKEN`, `PAYSTACK_SECRET_KEY`, `SENTRY_DSN`, etc.). Don't put secrets in the
GitHub Actions YAML in plain text.
