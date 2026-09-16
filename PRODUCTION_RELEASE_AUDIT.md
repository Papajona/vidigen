# Vidigen V12 Production Release Audit

Date: 2026-09-14

## Applied corrections
- Mandatory admin TOTP policy (`VIDIGEN_ADMIN_2FA_REQUIRED=true` by default).
- Real R2 prefix cleanup on admin user deletion plus protected manual cleanup endpoint.
- Real browser `/api/render` MP4 pipeline using FFmpeg/ffprobe and durable R2 output.
- Render source host allowlisting and private/reserved network rejection.
- Consistent audio-stream normalization during clip concatenation.
- Agent media QA now performs real ffprobe decode/duration/dimension checks.
- Agent runs persist to `agent_runs` when Supabase persistence is configured.
- Android native exporter targets H.264 for broad MP4 compatibility.
- Machine-specific Android `local.properties`, runtime memory, caches and compiled bytecode were removed from the deployable package.

## Verified in this isolated container
- Python syntax/compile: PASS
- Gateway/Agent/edit/reframe/2FA-independent Python tests: 32 PASS
- JavaScript tests: 4 PASS
- Real FFmpeg render with two source MP4 clips: PASS
- Real FFmpeg render with external audio track: PASS
- ffprobe verification of rendered video/audio streams: PASS
- Mandatory admin 2FA gate: PASS (428 until enrollment)
- Admin dashboard JavaScript syntax: PASS

## Not claimable from this container
- `pyotp` end-to-end test execution, because the isolated container does not contain the dependency and has no network access to install it. The project declares `pyotp>=2.9,<3`.
- `npm run build`, because `node_modules` is not present in this isolated package snapshot.
- Android Gradle release build, because the Gradle wrapper distribution must be downloaded and the Android SDK/keystore must exist on the deployment machine.
- Live Replicate/Seedance/Runway generation, Supabase production connectivity, R2 credentials, and real-world provider quotas because those require the deployment project's credentials.
- Database disaster recovery. Supabase-managed backups and a restore drill remain operational deployment requirements.
- Native Android/host crash telemetry; this requires deployment-platform telemetry configuration.

## Release interpretation
This package contains production code paths rather than a demo/prototype implementation. A public deployment should proceed only after the deployment-specific external checks above are completed successfully on the target environment.

## Billing, Subscription and Brain Learning update

The release now includes production-oriented billing primitives and Admin controls:
- Creator / Pro / Studio monthly subscriptions plus Free tier.
- GHS-denominated plan pricing and monthly credit allowances.
- Server-side credit ledger and per-model application credit weights.
- Paystack hosted checkout initialization, transaction verification, webhook signature verification and idempotent payment-event storage.
- Admin plan-price/credit/storage/entitlement adjustment with Paystack recurring-plan synchronization when configured.
- Google Pay checkout selection is routed through the configured hosted payment provider path and is disabled by default until the merchant account/provider capability is explicitly verified.
- Brain output learning samples generated media, records frame/media statistics, provenance prompts/tags and ratings, and stores reusable creative patterns.
- Agent generation retrieves user-owned learned creative patterns and can request multi-episode AI series generation through the canonical V12 generation router.

### Pricing note
The default GHS prices are internal commercial defaults, not provider-cost claims. Premium model consumption is controlled with model-specific credit weights which an administrator can adjust. Actual third-party model invoices must be monitored in the provider account and used to tune the internal credit weights.

### Google Pay verification limitation
Live browsing is unavailable in this audit environment, so the current Paystack documentation at the user-supplied URL was not re-read during this change. The integration therefore does not falsely assert universal Google Pay availability. The frontend can request a Google Pay checkout method, while the hosted Paystack checkout remains the payment path; enable the wallet only after confirming that the current merchant account/checkout exposes Google Pay.

### Release tests after billing/learning changes
- Python unittest suite: 31 tests, 30 passed, 1 explicitly skipped because `pyotp` is declared in `gateway/requirements.txt` but is unavailable in this isolated offline container.
- Gateway smoke tests: PASS.
- Node learning tests: 4/4 PASS.
- Runtime scan: no demo/prototype/simulation paths in `gateway/` or `src/`.
- Secret scan: no `.env`, private-key or populated provider-secret files included in the release archive.


## Production demo-layout audit (2026-09-14)
- Removed bundled public sample videos and Starter media cards from the user-facing frontend.
- Removed the untrusted Agent `echo` tool from the runtime registry.
- Media Library now uses user-imported/project-generated assets only.
- Provider registry is capability-based and only configured providers are eligible for execution.
- Development localhost values remain only in development configuration/documentation; production deployment must supply real gateway/origin values.

## Full user-facing demo-layout fact check (2026-09-14)
PASS — Removed bundled public sample videos and starter-media cards.
PASS — User Media Library is import/generation driven and has a real empty state.
PASS — User-facing source contains no localhost/127.0.0.1 runtime fallback.
PASS — User-facing source contains no demo/simulation/mock/starter-media copy.
PASS — No .env/private-key/PEM/secret files are packaged.
PASS — Node UI tests: 4/4 passed.
PASS — Python suite: 31 tests, 30 passed + 1 environment-only skip (pyotp unavailable offline).
NOTE — Internal Admin QA pages may contain test wording because they are operational test tools, not customer-facing demo content.
NOTE — Avatar remains provider-dependent; no fake avatar renderer was introduced.
