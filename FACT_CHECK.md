# Vidigen AI V12 — two-pass fact check and competitive audit

*(Originally written against V10.1; updated in place across v12 changes — the version
number wasn't kept in sync with `package.json` for a few iterations. Retitled here rather
than left stale, since shipping something labeled "production-studio" while its own audit
doc claimed to be about a different, older version was a real inconsistency worth fixing,
not just noting.)*

## Pass 1 — factual/implementation corrections

This build is a **testing candidate**, not feature-complete parity with CapCut, Runway, HeyGen, or Adobe. The UI intentionally borrows common product patterns (editor shell, inspector, timeline, AI assistant) but does not copy proprietary code or assets.

### Confirmed in source
- React/Vite web editor with Capacitor packaging configuration.
- Local preference memory stored in browser localStorage.
- Gateway authentication, CORS allowlist, rate limiting and security headers.
- Local faster-whisper caption endpoint with word timestamps when the gateway host has faster-whisper + ffmpeg installed.
- Server-side provider adapter architecture; provider credentials are not in frontend source.
- Supabase metadata schema with RLS policies.
- Demo MP4s are explicitly labelled as demo media.

### Corrections made for V10.1
- Removed the Vite build-time injection of `process.env.GEMINI_API_KEY`; the test browser key path uses only the explicit `VITE_GEMINI_API_KEY` variable.
- Changed provider labels so the UI no longer implies Replicate/Seedance/Runway are universally available or that one Replicate model covers every media task.
- Changed the product badge to TEST and clarified that the current timeline is not a full NLE.
- Changed export wording to make clear it exports the selected clip rather than rendering a complete edited timeline.
- Kept Gemini browser-key support explicitly development/testing-only.

### Important implementation limitations
- The timeline is a functional UI model, not yet a full non-linear editor: no true trim/split/ripple editing, keyframes, transitions, audio mixing, motion tracking, masking, color grading, or final timeline renderer.
- AI Avatar is a request mode, not a HeyGen-equivalent presenter system: no built-in avatar catalog, lip-sync renderer, voice cloning, scene compositing or provider-specific avatar workflow.
- AI Director scene breakdown is currently deterministic/local; it is not a trained autonomous director model.
- Replicate integration is generic and requires a compatible configured model/input contract. It is not a universal video/image API.
- Seedance and Runway are adapter slots only; no undocumented API endpoint is claimed.
- Cloudflare R2 is configuration/documentation, not a completed upload/CDN pipeline in this build.
- Supabase schema/persistence hooks exist, but complete production session refresh, project CRUD and job reconciliation are not complete.
- Quality evaluation/regeneration loop and LoRA training pipeline are not implemented.

## Pass 2 — competitive judgment

Scores below are for the **current testing build**, not the future roadmap. 10/10 means a mature product at that competitor's strongest workflow, not theoretical feature count.

| Dimension | CapCut editing | Runway generation | HeyGen avatar/presentation | Adobe creative control | Vidigen V10.1 |
|---|---:|---:|---:|---:|---:|
| Core workflow maturity | 9 | 9 | 9 | 10 | 4 |
| Timeline/NLE depth | 9 | 7 | 4 | 10 | 3 |
| Generative video workflow | 5 | 10 | 6 | 8 | 5 |
| Avatar/presenter workflow | 5 | 5 | 10 | 6 | 2 |
| Pro creative controls | 7 | 7 | 5 | 10 | 3 |
| Audio/captions | 9 | 7 | 9 | 10 | 4 |
| AI orchestration/learning | 6 | 8 | 7 | 8 | 6 |
| **Overall for this test build** | **8.0** | **8.0** | **7.0** | **9.0** | **3.9** |

These are engineering judgments based on established product positioning/workflows, **not a live September 2026 feature crawl**.

### Simultaneous verdict
- **CapCut:** Vidigen has the right shell but is far behind on editing depth and speed.
- **Runway:** Vidigen has the beginnings of the same request → generation concept, but its real provider orchestration and quality loop are not mature enough for parity.
- **HeyGen:** Vidigen is substantially behind on avatars/presenters.
- **Adobe:** Vidigen is substantially behind on professional editing, audio, color, compositing and finishing.

The strongest differentiator today is **AI Director + local preference Brain + provider-neutral architecture**, not raw editing or avatar capability.

## Verification limitation
This environment cannot operate the user's private Google AI Studio workspace, Android Studio, an emulator/device, or paid third-party provider accounts. Before release, run `npm install`, `npm test`, `npm run build`, gateway tests, and Android Studio device/emulator tests locally.

## Addendum — changes made after this audit was written
The scorecard above predates the following, so treat those specific rows as stale until
re-scored, not the whole document:
- Real local export via Media3 Transformer (native Android plugin, not compiled/tested in
  this environment — no Android SDK access here; verified only by reading against current
  Media3 docs and checking the surrounding JS builds cleanly).
- Cloudflare R2 upload pipeline (`/api/r2-presign`) — previously config-only, now implemented.
- Background removal (`/api/remove-background`, image + video via Replicate) — did not
  exist before; also fixed a real bug in its first version (missing server-side polling
  meant slow/video jobs could never report as finished).
- Imported local media now also gets copied to native storage (`@capacitor/filesystem`) so
  native export can actually read it — previously only worked for generated/sample clips.

None of this changes the CapCut/Runway/HeyGen/Adobe scores in the table above by more than
roughly half a point on the CapCut-editing and Adobe-professional-controls rows — it's real
progress, not a tier change.

## Second addendum — security fixes
- **Critical: JWT algorithm-confusion vulnerability in `_jwt_user()`.** `HS256` was included
  alongside `ES256`/`RS256` in the allowed algorithms for tokens verified against a
  JWKS-sourced (public) key. Since JWKS keys are public by design, allowing a symmetric
  algorithm meant anyone could forge a valid token for any user by signing it with the
  public key as an HMAC secret — a full authentication bypass. Fixed by restricting to
  `['ES256','RS256']`, the only algorithms a JWKS-sourced key is actually valid for.
- Gateway-token comparison (`token == GATEWAY_TOKEN`) switched to `secrets.compare_digest`
  to remove a timing side-channel.
- Removed a redundant synchronous JWKS fetch whose result was never used, and added
  module-level caching of the `PyJWKClient` so the JWKS document isn't re-fetched on every
  single authenticated request.
- Checked and confirmed NOT vulnerable: R2 object-key path traversal (S3-compatible storage
  doesn't resolve `../` hierarchically — it's a flat, opaque key namespace, so the
  `users/{uid}/` prefix can't be escaped this way regardless of client input) and
  authorization scoping on job/feedback writes (`user_id` is always taken from the
  server-verified JWT `sub` claim, never from client-supplied request bodies).


## Third addendum — AI Agent integration audit
- Added the AI Agent orchestrator as a layer above the existing gateway services rather than a parallel provider stack.
- Agent API routes inherit the gateway authentication dependency; the agent cannot be reached anonymously when the gateway is configured with a token/JWT requirement.
- Agent run execution is asynchronous at the API boundary and exposes run status/cancellation instead of holding a long provider request open.
- Agent generation delegates to `/api/generate` service logic and status polling delegates to the canonical generation status implementation.
- The memory acknowledgement bug was fixed: the `remember` tool now actually writes the event.
- Local memory writes are user-scoped and protected against concurrent file-write races within the process.
- Agent QA is intentionally conservative: it verifies generation completion and an output URL; it does not claim full visual QA until a real media-analysis worker exists.
- The Agent does not report successful video creation when the configured provider/workflow is unavailable; the integration test confirms a video goal fails rather than falsely completing.
- Web/Android build execution remains environment-blocked here by unavailable dependencies/network; source-level and offline test checks were still run.

## Fourth addendum — moderation/billing hardening after external review
A review of this package (not written by this environment) flagged four gaps. All four
have been addressed in source; what follows is a plain account of the change and what has
and hasn't been independently re-verified.

- **Output moderation failed OPEN by default when unconfigured.** Previously, a missing
  `REPLICATE_API_TOKEN` (or a failed/errored classifier call) resulted in `safe: True` —
  generations were delivered unchecked. Fixed: `gateway/moderation.py` now fails CLOSED by
  default (`VIDIGEN_MODERATION_ENFORCE=true`) — "the classifier couldn't run" now blocks
  the item instead of waving it through. Explicitly relaxable via
  `VIDIGEN_MODERATION_ENFORCE=false` for dev environments without moderation credentials;
  this must stay `true` (the default) for any deployment serving real users. Added to the
  `/api/admin/release-readiness` gate as `output_moderation_ready` so an unconfigured token
  shows up as a readiness failure, not a silent gap discovered via blocked-output tickets.
  Verified: `tests/test_moderation.py` now asserts both directions (enforced-blocks and
  explicitly-relaxed-allows) rather than only the old fail-open path.
- **Prompt keyword fallback was narrow.** The no-Groq fallback only matched CSAM-adjacent
  phrasing. Added patterns for weapon/explosive synthesis instructions, drug synthesis, and
  non-consensual sexual content — still a fallback, not a replacement for the Groq
  classifier, and still real about not catching obfuscation/non-English phrasing (see the
  module docstring). Verified: new fallback pattern tests in `tests/test_moderation.py`.
- **2FA verification had no account-level lockout**, only the gateway's IP-based rate
  limiter — weak against a 6-digit TOTP code if attempts are spread across IPs. Added
  `failed_attempts`/`locked_until` to `admin_2fa` (idempotent `ADD COLUMN IF NOT EXISTS`
  migration included in `supabase_schema.sql` for already-deployed databases), a
  configurable threshold/lockout window (`VIDIGEN_2FA_MAX_ATTEMPTS`,
  `VIDIGEN_2FA_LOCKOUT_MINUTES`, defaults 5/15), and wiring in
  `/api/admin/2fa/verify` to check-before-verify and record/reset on
  failure/success. Verified: `tests/test_2fa_lockout.py` covers the lock-state logic with
  `sb_request` mocked (not a live-Supabase test).
- **No idempotency key on `/api/generate`**, so a retried request (client retry, double
  network delivery) could bill and submit twice. Added an optional `idempotencyKey` field;
  when present and Supabase persistence is configured, the gateway looks up an existing job
  by (user, key) via the `request->>idempotencyKey` jsonb path (no new column/migration
  needed — the key was already stored in the persisted request payload) and replays that
  job instead of billing again. Frontend (`src/main.jsx`) now generates one key per logical
  generation call. This is best-effort, not a hard guarantee: there's no unique constraint
  backing the lookup, so two requests racing past the check within the same short window
  can still both land — closes the common retry case, not the theoretical race. Verified:
  `tests/test_idempotent_generate.py` covers the persistence lookup call shape and an
  ASGI-level replay of `/api/generate` with `persistence` mocked.

### Not independently re-verified
Same limitation as every other addendum in this document: this environment has no
`fastapi`/`httpx`/`pyotp`/`pytest` installed and no network access to install them, so none
of the new or updated tests above (`test_moderation.py`, `test_2fa_lockout.py`,
`test_idempotent_generate.py`) have actually been executed here — only confirmed to
`py_compile` cleanly and read line-by-line for correctness. Run the real suite
(`pip install -r gateway/requirements.txt && python3 -m pytest tests/`) before treating this
as verified. The `admin_2fa` schema migration (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`)
also needs to be applied to any already-provisioned Supabase project — it will not appear
automatically.
