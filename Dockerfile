# Cloud Run needs a real container image — this installs the actual OS-level ffmpeg binary
# that faster-whisper (captions), auto-reframe, and the render pipeline all shell out to via
# subprocess. That's exactly the thing Cloudflare Workers cannot do at all, and the reason
# this gateway needs a real container host rather than an edge-function platform.
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY gateway/requirements.txt gateway/requirements.txt
RUN pip install --no-cache-dir -r gateway/requirements.txt

COPY gateway/ gateway/
COPY supabase_schema.sql .

# Cloud Run injects the actual listen port via $PORT — do not hardcode 8787 here, or the
# container will fail Cloud Run's startup health check even though it's running fine.
ENV VIDIGEN_HOST=0.0.0.0
CMD exec uvicorn gateway.server:app --host 0.0.0.0 --port ${PORT:-8080}
