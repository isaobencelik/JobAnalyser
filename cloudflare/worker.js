// Cloudflare Worker: serves the Hugging Face Space at jobanalyser.obencelik.com.
// Visitors only ever see your domain. Live search streaming passes straight through.
//
// Set ORIGIN to your Space's direct URL: https://<hf-username>-jobanalyser.hf.space
// (shown on the Space page under "⋮ → Embed this Space").
const ORIGIN = 'https://REPLACE-ME-jobanalyser.hf.space';

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const target = new URL(url.pathname + url.search, ORIGIN);

    const headers = new Headers(request.headers);
    headers.delete('host');
    // Real visitor IP, so the app's per-visitor rate limits work behind the proxy.
    headers.set('X-Visitor-IP', request.headers.get('CF-Connecting-IP') || '');

    const init = { method: request.method, headers, redirect: 'manual' };
    if (request.method !== 'GET' && request.method !== 'HEAD') init.body = request.body;

    const upstream = await fetch(target.toString(), init);

    // Keep any redirects on our own domain instead of bouncing to hf.space.
    const response = new Response(upstream.body, upstream);
    const loc = response.headers.get('Location');
    if (loc && loc.startsWith(ORIGIN)) {
      response.headers.set('Location', loc.replace(ORIGIN, url.origin));
    }
    return response;
  },
};
