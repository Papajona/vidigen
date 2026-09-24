# Vidigen V10 gateway

Real captions use `faster-whisper` with word timestamps. The Whisper model is cached in-process after first load. FFmpeg is required by the media decoding stack.

```bash
pip install -r requirements.txt
ffmpeg -version
python -m uvicorn server:app --host 127.0.0.1 --port 8787
```

`GET /api/captions/health` reports caption-engine readiness. `GET /api/providers` reports server-side provider configuration.

Replicate has a concrete server-side adapter. Seedance and Runway are configurable provider slots; their exact API contracts are intentionally not guessed or embedded in the app.

## Internal Agent package

The gateway/agent/ package is retained as an internal orchestration scaffold. It is not mounted as a public /api/agent/* service in the current production gateway, and its placeholder tools are not advertised as production generation features. The production UI currently uses the authenticated generation, status, Brain and edit endpoints directly.

Do not add a public /api/agent/* route until its tools are wired to the same provider registry, billing, ownership checks and media QA used by the production generation path.
