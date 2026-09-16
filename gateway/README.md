# Vidigen V10 gateway

Real captions use `faster-whisper` with word timestamps. The Whisper model is cached in-process after first load. FFmpeg is required by the media decoding stack.

```bash
pip install -r requirements.txt
ffmpeg -version
python -m uvicorn server:app --host 127.0.0.1 --port 8787
```

`GET /api/captions/health` reports caption-engine readiness. `GET /api/providers` reports server-side provider configuration.

Replicate has a concrete server-side adapter. Seedance and Runway are configurable provider slots; their exact API contracts are intentionally not guessed or embedded in the app.

## AI Agent Orchestrator (V12)

The gateway now mounts `/api/agent/*` behind the existing gateway authentication dependency.

- `GET /api/agent/health` — authenticated health check.
- `GET /api/agent/tools` — authenticated list of allowed agent tools.
- `POST /api/agent/run` — create an asynchronous agent run (`wait=false` by default).
- `GET /api/agent/runs/{run_id}` — read a caller-owned run.
- `POST /api/agent/runs/{run_id}/cancel` — cancel a caller-owned run.

The agent delegates to the existing V12 analyzer, provider/generation service and generation-status implementation. It does not expose arbitrary shell/file/database tools. The current media quality gate is deliberately conservative and does not claim full visual QA.
