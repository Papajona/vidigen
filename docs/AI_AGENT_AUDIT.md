# Vidigen V12 AI Agent Layer — Implementation Audit

## Scope
The agent layer was integrated against the uploaded V12 gateway rather than treated as an independent stack.

## Corrections applied
- Agent routes are mounted behind the existing V12 `auth` dependency.
- Agent planning is fail-closed against unknown tools and has a maximum step limit.
- Agent execution is asynchronous at the API boundary (`POST /api/agent/run`) with run polling and cancellation endpoints.
- The agent delegates request analysis and video generation to the canonical V12 gateway services instead of duplicating provider code.
- Generation status is polled through the existing V12 `/api/status` implementation.
- Agent memory writes are real writes, not acknowledgements, and include user scope in the local fallback store.
- Local fallback memory writes use a lock and atomic replacement to reduce concurrent-write corruption.
- Media QA does not claim visual quality that is not implemented; the current gate requires a successful generation status and output URL.
- Planning blocks credential/secret and explicit abuse requests.
- Supabase `agent_runs` schema is provided for production persistence, while the current runtime uses bounded in-memory run state plus existing `generation_jobs`.
- A regression test suite was added for planner safety, retries, memory, auth, API routing and run lifecycle.

## Known limitations
- The current media QA does not yet inspect decoded frames, audio tracks, captions, visual prompt adherence, flicker or motion artifacts. Those require a media-analysis worker.
- Provider generation is only a real operation when a V12 provider is configured and accessible. Offline tests intentionally use deterministic simulated tools.
- The API run registry is process-local; multi-instance deployment needs `agent_runs` persistence plus a queue/worker.
- Web/Android compilation could not be completed in the audit container because dependencies/Gradle distribution were unavailable offline.

## Verified in this environment
- Python compileall: PASS
- Gateway smoke test: 4/4 PASS
- Agent Core tests: 4/4 PASS
- Agent API tests: 2/2 PASS
- Edit-operation tests: 12/12 PASS
- Reframe tests: 8/8 PASS
- Frontend Node tests: 4/4 PASS
- Invalid video generation environment fails closed rather than falsely returning a completed agent run.

## Environment-only blockers
- Full Python suite includes TOTP tests requiring `pyotp`; dependency is declared in `gateway/requirements.txt`, but this container cannot reach PyPI to install it.
- `npm run build` requires Vite in `node_modules`; the uploaded project has no installed `node_modules` and network access is unavailable.
- Android Gradle compile requires the Gradle 8.11.1 distribution and Android SDK artifacts; the wrapper attempted to download Gradle and was blocked by network access.


## Production hardening pass 2
- Admin 2FA is mandatory when `VIDIGEN_ADMIN_2FA_REQUIRED=true` (default).
- Browser export now uses `/api/render`, real FFmpeg rendering, and durable R2 output.
- Render sources are host-allowlisted and private/reserved IP targets are rejected to reduce SSRF risk.
- Each render part is normalized with a consistent audio stream before concatenation.
- Agent generation QA now uses ffprobe to verify that the output is decodable video with valid dimensions and positive duration.
- Agent runs are persisted to `agent_runs` when Supabase is configured; local memory remains the development/offline fallback only.
