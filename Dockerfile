# Vidigen gateway — Cloud Run image
#
# Build context is the REPO ROOT (not gateway/), because gateway/server.py imports itself
# as `gateway.server:app` / `from gateway import persistence` — it relies on `gateway/`
# being an importable top-level package from the working directory. Keep that layout.
#
#   docker build -t vidigen-gateway -f Dockerfile .
#
FROM python:3.12-slim

# ffmpeg: used directly by /api/render (subprocess ffmpeg/ffprobe) and by faster-whisper's
# audio decoding path. curl: only for the container-level HEALTHCHECK below.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so this layer is cached across code-only changes.
COPY gateway/requirements.txt gateway/requirements.txt
RUN pip install --no-cache-dir -r gateway/requirements.txt

# Bake the faster-whisper model into the image at build time. Without this, the FIRST
# request after every cold start (new revision, scale-from-zero, or instance restart)
# would block on downloading the model from Hugging Face — slow, and a runtime dependency
# on an external host you don't control. Baking it in trades a slower image build for a
# fast, self-contained runtime. Must match WHISPER_MODEL at runtime or it re-downloads.
ARG WHISPER_MODEL=small
RUN python -c "from faster_whisper import WhisperModel; WhisperModel('${WHISPER_MODEL}', device='cpu', compute_type='int8')"
ENV WHISPER_MODEL=${WHISPER_MODEL}

COPY gateway/ gateway/

ENV PYTHONUNBUFFERED=1 \
    VIDIGEN_HOST=0.0.0.0

# Cloud Run injects $PORT at runtime (currently 8080) — never hardcode the port.
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD curl -f "http://127.0.0.1:${PORT:-8080}/api/health" -H "Authorization: Bearer ${VIDIGEN_GATEWAY_TOKEN}" || exit 1

CMD ["sh", "-c", "uvicorn gateway.server:app --host 0.0.0.0 --port ${PORT:-8080}"]
