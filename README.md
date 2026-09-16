# Vidigen AI

Vidigen is an AI-native video production studio: describe what you want, get multi-shot
AI-generated footage, then edit it on a real timeline — trim, reorder, color, speed,
captions, audio — before rendering a final video. It's built as two cooperating pieces: a
React/Vite web app for the editor UI, and a Python gateway that talks to AI generation
providers, runs FFmpeg renders, transcribes captions, and handles billing — so that
provider API keys and payment secrets never live in the browser or the Android app.

**Live workflow:** Idea → AI Director → shot graph → provider routing → generation jobs →
quality gate → timeline → AI editing → captions/audio → render.

## What's in this repository

| | |
|---|---|
| **Frontend** (`src/`) | React 19 + Vite web editor: timeline, AI Director, AI Editor (natural-language edit commands), color/transform/speed controls, text overlays, caption styling, billing UI. Also packaged as an Android app via Capacitor (`android/`). |
| **Gateway** (`gateway/`) | FastAPI backend: provider routing (Replicate, with Seedance/Runway as configurable slots), real caption transcription via `faster-whisper`, server-side FFmpeg rendering, Paystack billing + credit ledger, Supabase-backed persistence, admin dashboard with mandatory 2FA, an AI agent orchestrator for multi-step generation runs. |

## Features

- AI Director multi-shot generation with provider abstraction and an auto-router
- Structured, editable timeline — undo/redo, split, duplicate, reorder, delete, keyframes
- Natural-language AI Editor for timeline commands
- Professional color, transform, speed, and opacity controls; text overlays
- Audio import and microphone recording
- Real speech-to-text captions (`faster-whisper`) with SRT export
- Creative preference memory that learns from a user's own generated output
- Subscription billing (Paystack) with a server-side credit ledger
- Admin dashboard: health checks, feature flags, audit log, remote config, mandatory
  TOTP 2FA on every admin route
- A server-side render pipeline (real FFmpeg, not a stub) with SSRF-hardened source
  fetching
- Android packaging via Capacitor, with a native media-engine boundary for future
  Media3/MediaCodec/Vulkan work

See `V12_IMPLEMENTATION.md` and `docs/V12_ARCHITECTURE.md` for the deeper technical
writeup, and `docs/AI_AGENT_AUDIT.md` for the agent orchestrator specifically.

## Getting started locally

### Frontend

```bash
npm install
npm run dev          # local dev server
npm test             # dependency-free Node test suite
npm run build         # production build (outputs to dist/)
```

Point it at a gateway by setting `VITE_VIDIGEN_GATEWAY_URL` (see `.env.example`).

### Gateway

```bash
cd gateway
pip install -r requirements.txt
ffmpeg -version       # required on PATH
python -m uvicorn server:app --reload --port 8787
```

Copy `.env.example` to `.env` and fill in what you need — nothing is required to run
locally except `VIDIGEN_GATEWAY_TOKEN` once you're not on `127.0.0.1`. Every other
integration (Supabase, R2, Replicate, Paystack, Groq) is optional and degrades gracefully
when unconfigured; `GET /api/admin/release-readiness` reports exactly what's live.

### Android

```bash
npm run android:sync
npm run android:open
```

Details and constraints in `ANDROID.md`.

## Testing

```bash
npm test                                  # Node/JS tests
python3 -m pytest tests/                  # Python gateway tests
```

## Deployment

This app deploys as two independent services — the frontend to Cloudflare Pages, the
gateway to Google Cloud Run — connected through GitHub Actions. The complete, step-by-step
walkthrough (GCP setup, secrets, DNS, first deploy, pre-launch checklist) is in
[`DEPLOYMENT.md`](./DEPLOYMENT.md).

## Security

- No provider or payment secrets are ever bundled into the browser or the Android app —
  see `SECURITY.md` and `PRODUCTION_ROADMAP.md`.
- Admin routes require TOTP 2FA by default (`VIDIGEN_ADMIN_2FA_REQUIRED`).
- CORS is an explicit allowlist, never a wildcard.
- Server-side rendering validates and resolves every source URL to reject private/internal
  network addresses before fetching (SSRF protection).

If you find a security issue, please don't open a public issue — see `SECURITY.md` for
how to report it.

## Project layout

```
src/            React/Vite frontend
gateway/        FastAPI backend (providers, billing, captions, render, admin, agent)
android/        Capacitor Android project
native-media-engine/   Native Android media-engine boundary (Kotlin/C++)
supabase/       Database schema
tests/          Python + JS test suites
docs/           Architecture and agent-orchestrator deep-dives
```

## License

No license file is currently included, which means default copyright applies — the code
is not licensed for reuse. Add a `LICENSE` file here once you've decided how you want
this repository to be shared (or keep it proprietary and remove this note).
