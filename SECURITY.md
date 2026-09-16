# Vidigen AI Security Baseline

- No provider API keys are injected into the browser bundle. Cloud AI must use a server-side proxy with secret storage.
- Local gateway requires loopback access by default; set `VIDIGEN_GATEWAY_TOKEN` before allowing LAN/tunnel access.
- CORS uses an explicit allowlist (`VIDIGEN_ALLOWED_ORIGINS`), not `*`.
- Gateway rate limits API calls and adds common security headers.
- Inputs have bounded lengths and strict formats; prompt IDs are validated.
- Do not expose ComfyUI directly to the internet. Put the gateway behind authentication and, for remote access, TLS/VPN or a hardened reverse proxy.
- Android should use HTTPS for remote gateways. Avoid cleartext traffic in production.
- Treat imported workflows/models as untrusted code until reviewed.
