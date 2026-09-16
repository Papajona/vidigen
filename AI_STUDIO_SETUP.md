# Google AI Studio testing — Vidigen AI V10.1

This is a React/Vite **web testing build** intended to be imported into a Google AI Studio development workspace when that workspace supports the repository's build/runtime flow. It is not an Android APK.

## Gemini test mode
Set a development-only browser variable if your AI Studio environment provides one:

```text
VITE_GEMINI_API_KEY=YOUR_TEST_KEY
VITE_GEMINI_MODEL=gemini-2.5-flash
```

The key is bundled into the browser build by design for this test path. **Do not publish that build or reuse the key for production.** Production Gemini/provider calls should move behind an authenticated server-side gateway or an approved Google client/auth mechanism.

## What AI Studio can test without a gateway
- Editor shell and responsive UI
- Gemini Analyze/Optimize (if the test key is available and the selected model is enabled for the account)
- Local Brain/preferences
- Demo media
- Timeline selection/duplicate/delete/zoom
- Audio import/microphone
- Caption UI and SRT generation

## What still needs the gateway/provider
- Real video/image generation
- faster-whisper transcription
- Replicate jobs
- Seedance/Runway adapters
- Supabase server persistence
- R2 storage

## Hosted AI Studio networking
A hosted browser normally cannot reach `127.0.0.1` on the developer computer. For end-to-end generation, expose the gateway through a secure authenticated HTTPS endpoint and allow that exact browser origin in the gateway CORS configuration.
