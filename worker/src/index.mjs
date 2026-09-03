const EVENT_ASSET = /^events\/third-castle\/[a-f0-9]{20}\.webp$/;
const MANIFEST = "manifests/third-castle/current.json";

export function keyFromPathname(pathname) {
  let key;
  try {
    key = decodeURIComponent(pathname).replace(/^\/+/, "");
  } catch {
    return null;
  }
  return EVENT_ASSET.test(key) || key === MANIFEST ? key : null;
}

function plain(status, message) {
  return new Response(message, {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "text/plain; charset=utf-8",
      "X-Content-Type-Options": "nosniff",
    },
  });
}

function responseHeaders(object, key) {
  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("ETag", object.httpEtag);
  headers.set("Access-Control-Allow-Origin", "*");
  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Content-Security-Policy", "default-src 'none'");
  headers.set(
    "Cache-Control",
    key === MANIFEST ? "no-store" : "public, max-age=31536000, immutable",
  );
  return headers;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      return plain(405, "Method not allowed");
    }

    const url = new URL(request.url);
    const key = keyFromPathname(url.pathname);
    if (!key) return plain(404, "Not found");

    // A query string can never bypass the cache or multiply R2 reads.
    const canonicalUrl = new URL(url);
    canonicalUrl.search = "";
    const cacheKey = new Request(canonicalUrl.toString(), { method: "GET" });
    const cache = caches.default;

    if (request.method === "GET" && key !== MANIFEST) {
      const cached = await cache.match(cacheKey);
      if (cached) return cached;
    }

    // Exactly one private-bucket operation is possible for an accepted request.
    const object = request.method === "HEAD"
      ? await env.EVENT_IMAGES.head(key)
      : await env.EVENT_IMAGES.get(key);
    if (!object) return plain(404, "Not found");

    const response = new Response(
      request.method === "HEAD" ? null : object.body,
      { headers: responseHeaders(object, key) },
    );
    if (request.method === "GET" && key !== MANIFEST) {
      ctx.waitUntil(cache.put(cacheKey, response.clone()));
    }
    return response;
  },
};
