# Vidigen AI — V9 production roadmap

## Implemented in this build
- Real microphone/file audio ingestion in the editor.
- Real caption API contract using local `faster-whisper`; no fabricated captions.
- Timed caption segments, live preview overlay, caption style controls and SRT export.
- Provider-neutral model routing UI for Replicate, Seedance 2.5 and Runway.
- Supabase metadata schema with RLS and provenance fields.
- Cloudflare R2 is the recommended media object store/CDN; media should be referenced by storage keys in Supabase.
- Brain memory remains separate from foundation-model training.

## Production sequence
1. Install `faster-whisper` + ffmpeg on the gateway and validate transcription quality.
2. Add Supabase Auth/session exchange and signed R2 upload/download URLs.
3. Implement Replicate adapter with server-side token and webhook/job reconciliation.
4. Implement Seedance adapter only against its currently documented, authorized API/access path.
5. Implement Runway adapter as an optional premium provider.
6. Build shot graph + multi-track timeline commands (trim, split, ripple, keyframes, captions, audio mixing).
7. Add quality evaluation and regeneration loop.
8. Add curated LoRA training datasets with provenance/license validation and champion/rollback versions.
9. Add local model adapters through ComfyUI/Ollama.
10. Package with Capacitor for Android after the web workflow passes the V8/V9 acceptance suite.

## Security rule
No Replicate, Runway, Seedance, Supabase service-role, or other privileged secret belongs in browser JavaScript, Capacitor assets, or an APK.
