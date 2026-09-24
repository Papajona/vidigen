# Vidigen deployment runbook

Two independent deployables, two independent pipelines:

- **Frontend** (Vite/React static build) → Cloudflare Worker, deployed from GitHub Actions.
- **Gateway** (FastAPI + ffmpeg + faster-whisper, `gateway/server.py`) → Google Cloud Run,
  deployed by `.github/workflows/deploy-gateway.yml` on every push to `main`.

Your domain stays on Cloudflare throughout — DNS only changes, hosting doesn't move.

---

## 0. Files this adds to your repo

Copy these into the repo root, preserving paths:

```
Dockerfile
.dockerignore
.github/workflows/deploy-gateway.yml
.github/workflows/deploy-frontend.yml   (optional — see its header comment)
```

---

## 1. One-time GCP setup

You need: a GCP project with billing enabled, and the `gcloud` CLI logged in locally once
to run these setup commands. After this section, GitHub does the rest on every push.

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"          # pick the region closest to your users
export REPO_NAME="you/vidigen"       # GitHub org/repo, e.g. jane/vidigen

gcloud config set project "$PROJECT_ID"

# APIs this needs
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  iamcredentials.googleapis.com \
  secretmanager.googleapis.com

# Image storage
gcloud artifacts repositories create vidigen \
  --repository-format=docker \
  --location="$REGION"
```

### 1a. Keyless auth from GitHub Actions (Workload Identity Federation)

This lets GitHub Actions deploy to Cloud Run **without** a long-lived service-account JSON
key sitting in a GitHub secret — GitHub proves its identity to Google directly per run.

```bash
gcloud iam workload-identity-pools create "github-pool" \
  --location="global" \
  --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc "github-provider" \
  --location="global" \
  --workload-identity-pool="github-pool" \
  --display-name="GitHub OIDC" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --issuer-uri="https://token.actions.githubusercontent.com"

gcloud iam service-accounts create "vidigen-deployer" \
  --display-name="Vidigen GitHub deployer"

# Only THIS repo may impersonate the service account — not "any GitHub Actions run anywhere"
gcloud iam service-accounts add-iam-policy-binding \
  "vidigen-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')/locations/global/workloadIdentityPools/github-pool/attribute.repository/${REPO_NAME}"

# Permissions the deployer needs
for role in roles/run.admin roles/artifactregistry.writer roles/iam.serviceAccountUser roles/secretmanager.secretAccessor; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:vidigen-deployer@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$role"
done

# Values you'll paste into GitHub secrets (below)
gcloud iam workload-identity-pools providers describe "github-provider" \
  --location=global --workload-identity-pool="github-pool" --format="value(name)"
# -> GCP_WIF_PROVIDER
echo "vidigen-deployer@${PROJECT_ID}.iam.gserviceaccount.com"
# -> GCP_WIF_SERVICE_ACCOUNT
```

### 1b. Secrets in Secret Manager

Anything that's an actual credential goes here, not in plain env vars in the workflow.
Create each one (leave a secret empty/omit it if you're not using that provider yet —
just delete its line from `--set-secrets` in the workflow):

```bash
printf '%s' 'REPLACE_ME' | gcloud secrets create vidigen-gateway-token --data-file=-
printf '%s' 'REPLACE_ME' | gcloud secrets create supabase-service-role-key --data-file=-
printf '%s' 'REPLACE_ME' | gcloud secrets create replicate-api-token --data-file=-
printf '%s' 'REPLACE_ME' | gcloud secrets create paystack-secret-key --data-file=-
printf '%s' 'REPLACE_ME' | gcloud secrets create r2-access-key-id --data-file=-
printf '%s' 'REPLACE_ME' | gcloud secrets create r2-secret-access-key --data-file=-
printf '%s' 'REPLACE_ME' | gcloud secrets create groq-api-key --data-file=-   # optional
```

`VIDIGEN_GATEWAY_TOKEN` should be a long random value — e.g. `openssl rand -hex 32` — not
a memorable password; it's the bearer token every gateway API call authenticates with
until you also wire up Supabase JWT auth.

---

## 2. GitHub repository secrets

Settings → Secrets and variables → Actions, add:

| Secret | Value |
|---|---|
| `GCP_PROJECT_ID` | your GCP project id |
| `GCP_REGION` | e.g. `us-central1` |
| `GCP_WIF_PROVIDER` | output of the `providers describe` command above |
| `GCP_WIF_SERVICE_ACCOUNT` | `vidigen-deployer@<project>.iam.gserviceaccount.com` |
| `VIDIGEN_ALLOWED_ORIGINS` | `https://vidigen.online` (comma-separate if you also serve a staging origin) |
| `VIDIGEN_RENDER_ALLOWED_HOSTS` | the hostname your R2 public bucket serves from |
| `SUPABASE_URL` | `https://YOUR_PROJECT.supabase.co` |
| `R2_ENDPOINT`, `R2_BUCKET`, `R2_PUBLIC_BASE_URL` | from your Cloudflare R2 bucket settings |
| `VITE_VIDIGEN_GATEWAY_URL` | `https://api.vidigen.online` (used by the frontend build) |
| `VITE_SENTRY_DSN` | optional |
| `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` | only if using `deploy-frontend.yml` instead of the Pages dashboard |

Push to `main` and `deploy-gateway.yml` runs: installs deps, runs `tests/`, builds the
Docker image, pushes it to Artifact Registry, deploys to Cloud Run. It prints the
`*.run.app` URL and a ready-made `curl` command against `/api/admin/release-readiness` —
run that after every deploy; it's the gateway checking its own ffmpeg/whisper/Supabase/R2/
provider config live, more trustworthy than reading docs.

---

## 3. Cloudflare — DNS and Worker frontend

### Frontend: Cloudflare Worker

GitHub Actions runs `.github/workflows/deploy-frontend.yml`: `npm ci` → `npm test` → `npm run build` → `wrangler@4 deploy` using `wrangler.jsonc` and the `CLOUDFLARE_API_TOKEN` / `CLOUDFLARE_ACCOUNT_ID` secrets.

The Worker serves the built `dist/` assets and uses the SPA fallback configured in `wrangler.jsonc`. Add the Worker/domain route you want in the Cloudflare dashboard and point your public site hostname to this Worker.

### Gateway: point a subdomain at Cloud Run

```bash
gcloud run domain-mappings create \
  --service=vidigen-gateway \
  --domain=api.vidigen.online \
  --region="$REGION"
```

This prints a DNS record (a `CNAME` to `ghs.googlehosted.com`, or `A`/`AAAA` records for
an apex). Add exactly that record in the Cloudflare DNS dashboard for `api`.

- **Set it to "DNS only" (grey cloud) at first.** Cloud Run's domain mapping needs to see
  your real DNS to issue and verify the TLS certificate; Cloudflare's proxy will interfere
  with that handshake until the mapping is verified.
- Once `gcloud run domain-mappings describe --domain=api.vidigen.online --region="$REGION"`
  shows the certificate as ready, you can switch the record to "Proxied" (orange cloud) if
  you want Cloudflare's WAF/caching in front of the API too — optional, not required.

---

## 4. Cloud Run settings, and why they're set this way for THIS app

The workflow deploys with `--memory=4Gi --cpu=2 --no-cpu-throttling --concurrency=4
--min-instances=1 --max-instances=1`. Don't loosen these without reading why first:

- **`--no-cpu-throttling`**: Cloud Run normally throttles CPU to near-zero outside of
  active request handling. This app does real CPU work *inside* a request (ffmpeg
  transcode, faster-whisper transcription) — default throttling would make renders and
  captions time out or crawl.
- **`--concurrency=4`**: Cloud Run's default of 80 assumes an I/O-bound app. Four ffmpeg
  renders fighting over 2 vCPUs at once already saturates it; more concurrent requests
  just queue and risk hitting `VIDIGEN_RENDER_TIMEOUT`.
- **`--min/max-instances=1`**: `server.py`'s rate limiter and job caches
  (`OUTPUT_CAPS`, `JOB_CACHE`, `WHISPER_CACHE`) are plain in-process Python dicts — not
  Redis, not shared. Two Cloud Run instances means two independent rate limits and two
  independent job caches, silently, with no error. Stay at 1 instance until that's moved
  to a shared store; `min-instances=1` also avoids a cold start reloading the (already
  image-baked) Whisper model on every scale-from-zero.
- **`--memory=4Gi`**: Cloud Run's writable filesystem is in-memory (tmpfs) — the temp
  `.mp4` parts `_run_ffmpeg_render` writes during a render count against this same memory
  budget, on top of the Whisper model and Python runtime. Watch `VIDIGEN_MAX_RENDER_BYTES`
  × `VIDIGEN_MAX_RENDER_CLIPS` against this if you raise either.
- **`--timeout=900`**: matches the app's own `VIDIGEN_RENDER_TIMEOUT` default. If you
  raise `VIDIGEN_RENDER_TIMEOUT`, raise this too (Cloud Run allows up to 3600).

## 5. Before calling it launched

- [ ] `curl` the `/api/admin/release-readiness` URL the workflow prints — confirm `ready: true`.
- [ ] Confirm `VIDIGEN_PAYMENT_TEST_MODE=false` and `GOOGLE_PAY_TEST_MODE=false` actually
      took effect (they default to `true` in `.env.example` — the workflow above sets them
      to `false` explicitly, but double-check after first deploy).
- [ ] Confirm `VIDIGEN_ADMIN_2FA_REQUIRED=true` and that you've enrolled 2FA on the admin
      account before anyone else can reach `/api/admin/*`.
- [ ] Verify `VIDIGEN_ALLOWED_ORIGINS` is your real domain, not a wildcard or a leftover
      `localhost` entry.
- [ ] Hit `https://api.vidigen.online/api/health` with your gateway token and confirm the
      cert and DNS mapping are actually serving through the custom domain, not just the
      raw `*.run.app` URL.
