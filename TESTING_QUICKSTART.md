# Vidigen AI — Testing Quick Start

## 1. Web UI

```bash
npm install
npm run dev
```

Open the Vite URL, normally `http://localhost:5173`.

Click **Settings → Test connection** after starting the gateway.

## 2. Gateway smoke test (no AI provider required)

Install gateway dependencies:

```bash
python3 -m pip install -r gateway/requirements.txt
```

Then:

```bash
python3 tests/run_smoke.py
```

Expected: `4/4 smoke checks passed`.

## 3. Start gateway

```bash
export VIDIGEN_GATEWAY_TOKEN=test-token
python3 run_gateway.py
```

The gateway listens on `http://127.0.0.1:8787` by default.

## 4. Real generation

A real render requires one configured server-side provider:
- Replicate with `REPLICATE_API_TOKEN` + `REPLICATE_MODEL`, or
- Local ComfyUI with an exported `workflow_api.json`.

Do not put provider secrets in the browser, APK, or Git repository.

## 5. Captions

Real captions require `faster-whisper` and FFmpeg on the gateway host. The app intentionally reports an error instead of fabricating captions when the engine is unavailable.

## 6. Production persistence

For Supabase persistence, apply `supabase_schema.sql`, configure the Supabase URL/service-role key on the gateway, and configure JWT issuer/audience. Keep the service-role key server-side only.

## 7. Android

This repository is Capacitor-ready. After installing dependencies:

```bash
npm run build
npx cap add android
npm run android:sync
npx cap open android
```

Android Studio/device testing must be performed on a machine with Android SDK/Gradle tooling. The repository does not claim an Android build was executed here.
