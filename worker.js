const API_ORIGIN = 'https://vidigen-gateway-xvpegaghzq-uc.a.run.app';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Same-origin browser health probe. This avoids browser CORS/preflight issues for
    // the status indicator while still checking the real production Cloud Run gateway.
    if (url.hostname === 'vidigen.online' || url.hostname === 'www.vidigen.online') {
      if (url.pathname === '/__gateway_health') {
        const upstream = new URL('/healthz', API_ORIGIN);
        const response = await fetch(new Request(upstream, request), {
          cf: { cacheTtl: 0, cacheEverything: false },
        });
        return new Response(response.body, {
          status: response.status,
          statusText: response.statusText,
          headers: {
            'Content-Type': 'application/json',
            'Cache-Control': 'no-store',
          },
        });
      }
    }

    // api.vidigen.online is a Cloudflare Worker custom-domain proxy to the
    // already-verified Cloud Run gateway. This keeps the public API hostname
    // independent of Google Cloud domain ownership verification.
    if (url.hostname === 'api.vidigen.online') {
      const target = new URL(url.pathname + url.search, API_ORIGIN);
      const proxyRequest = new Request(target, request);
      return fetch(proxyRequest, {
        cf: {
          cacheTtl: 0,
          cacheEverything: false,
        },
      });
    }

    return env.ASSETS.fetch(request);
  },
};
