const API_ORIGIN = 'https://vidigen-gateway-xvpegaghzq-uc.a.run.app';

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

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
