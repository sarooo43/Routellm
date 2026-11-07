/**
 * Cloudflare Worker entrypoint for the RouteLLM dashboard.
 *
 * Architecture (Problem 2 fix — "503 on our own domain"):
 *
 *   Browser → Cloudflare Worker (this file) → Hugging Face Space (FastAPI)
 *           → Neon Postgres + Upstash Redis
 *
 * Same-origin requests to /api/*, /v1/*, /health, /docs… are proxied
 * server-to-server to the Space, so the browser NEVER talks to the backend
 * cross-origin and there is no VITE_API_BASE_URL to misconfigure. The backend
 * origin comes from the plain (non-secret) BACKEND_URL variable; the function
 * is the default so an unconfigured deploy still proxies somewhere sane.
 *
 * A sleeping (cold) HF Space can take up to a minute to answer, so:
 *  - every upstream call has a ~60s timeout,
 *  - GET requests are automatically retried ONCE on 502/503/504 or network
 *    errors (waking a Space is a retry-able condition, not a failure),
 *  - when the upstream is still unreachable the caller gets a structured
 *    503 {"detail": "...", "upstream": "down"} with the
 *    x-routellm-source: worker-proxy response header, so DevTools can always
 *    tell where an error came from. Successfully proxied responses carry
 *    x-routellm-source: backend.
 *
 * Security: headers/bodies are never logged anywhere in this module.
 */

const DEFAULT_BACKEND_URL = "https://saboornotgonna-routellm.hf.space";
const UPSTREAM_TIMEOUT_MS = 60_000;
const RETRYABLE_STATUSES = new Set([502, 503, 504]);

const API_PREFIXES = ["/api/", "/v1/"];
const API_EXACT_PATHS = new Set(["/health", "/docs", "/openapi.json", "/redoc"]);

/** Hop-by-hop headers (RFC 7230 6.1) — never forwarded through a proxy. */
const HOP_BY_HOP = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

/** Return true when a request path belongs to the FastAPI gateway. */
export function isApiPath(pathname) {
  return (
    API_PREFIXES.some((prefix) => pathname.startsWith(prefix)) ||
    API_EXACT_PATHS.has(pathname) ||
    pathname.startsWith("/docs/")
  );
}

/** BACKEND_URL wins; legacy ROUTELLM_API_ORIGIN still honored; then default.
 * Blank/invalid candidates fall through to the next one; non-http(s) values
 * are rejected (null → caller answers upstream-down). Never logs values. */
export function resolveBackendOrigin(env) {
  const candidates = [env.BACKEND_URL, env.ROUTELLM_API_ORIGIN, DEFAULT_BACKEND_URL];
  for (const candidate of candidates) {
    const cleaned = (candidate || "").trim().replace(/\/+$/, "");
    if (!cleaned) {
      continue;
    }
    try {
      const url = new URL(cleaned);
      if (url.protocol === "https:" || url.protocol === "http:") {
        return url.origin; // no credentials, no path components
      }
    } catch {
      // try the next candidate
    }
  }
  return null;
}

function upstreamDown() {
  const headers = new Headers({ "content-type": "application/json" });
  headers.set("x-routellm-source", "worker-proxy");
  return new Response(
    JSON.stringify({
      detail: "Backend unreachable or waking up",
      upstream: "down",
    }),
    { status: 503, headers },
  );
}

function withSourceHeader(response, source) {
  const headers = new Headers(response.headers);
  headers.set("x-routellm-source", source);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

/** Build ONE upstream request. Buffered body so every attempt can replay it
 * (API payloads are JSON-sized; no streaming uploads to support). */
async function buildUpstreamRequest(request, origin) {
  const url = new URL(request.url);
  const target = `${origin}${url.pathname}${url.search}`;

  const headers = new Headers(request.headers);
  headers.delete("host");
  for (const name of [...headers.keys()]) {
    if (HOP_BY_HOP.has(name.toLowerCase())) {
      headers.delete(name);
    }
  }
  // Identify the original browser origin for the backend (rate limiting,
  // logging). No secrets involved.
  headers.set("X-Forwarded-Proto", url.protocol.replace(":", ""));
  headers.set("X-Forwarded-Host", url.host);
  const clientIp = request.headers.get("CF-Connecting-IP");
  if (clientIp) {
    headers.set("X-Forwarded-For", clientIp);
  }

  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  return new Request(target, {
    method: request.method,
    headers,
    body: hasBody ? await request.arrayBuffer() : undefined,
    redirect: "manual",
    signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (!isApiPath(url.pathname)) {
      return env.ASSETS.fetch(request);
    }

    const origin = resolveBackendOrigin(env);
    if (!origin) {
      return upstreamDown();
    }

    // A sleeping Space needs time to wake: give GETs one automatic retry.
    const attempts = request.method === "GET" ? 2 : 1;

    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      let upstream;
      try {
        upstream = await buildUpstreamRequest(request, origin);
      } catch {
        return upstreamDown();
      }

      try {
        let response = await fetch(upstream);
        // Hugging Face can answer a *.hf.space URL with a redirect to the
        // canonical host (e.g. trailing-slash normalization). A proxy must
        // not forward that: the browser would be sent to the Space's own
        // domain, bypassing the Worker and the CORS contract. Follow the
        // hop server-side instead — for GET/HEAD there is no body to
        // replay, so rebuild from the original request cleanly.
        if (response.status >= 300 && response.status < 400) {
          const location = response.headers.get("location");
          if (location) {
            const nextOrigin = new URL(location, origin).origin;
            if (
              nextOrigin !== origin &&
              (request.method === "GET" || request.method === "HEAD")
            ) {
              upstream = await buildUpstreamRequest(request, nextOrigin);
              response = await fetch(upstream);
            }
          }
        }
        const retryable =
          attempt < attempts && RETRYABLE_STATUSES.has(response.status);
        if (retryable) {
          continue; // response discarded; the second attempt decides
        }
        return withSourceHeader(response, "backend");
      } catch {
        if (attempt < attempts) {
          continue; // network error / timeout — try once more (space waking)
        }
      }
    }

    return upstreamDown();
  },
};
