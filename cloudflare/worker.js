/**
 * Cloudflare Worker entrypoint for the RouteLLM dashboard.
 *
 * Cloudflare Workers cannot run the Python FastAPI gateway, PostgreSQL, or
 * Redis. This Worker serves the built React assets and optionally reverse-
 * proxies RouteLLM API paths to an externally hosted backend origin.
 *
 * Set ROUTELLM_API_ORIGIN to the HTTPS origin of the FastAPI gateway, e.g.
 * `https://api.example.com` (no path). If it is unset, API paths return a
 * structured 503 instead of accidentally serving the SPA index.html.
 */

const API_PREFIXES = ["/api/", "/v1/"];
const API_EXACT_PATHS = new Set(["/health", "/docs", "/openapi.json", "/redoc"]);

/** Return true when a request path belongs to the FastAPI gateway. */
export function isApiPath(pathname) {
  return (
    API_PREFIXES.some((prefix) => pathname.startsWith(prefix)) ||
    API_EXACT_PATHS.has(pathname) ||
    pathname.startsWith("/docs/")
  );
}

function apiMisconfigured(detail) {
  return Response.json(
    {
      error: "routellm_backend_not_configured",
      detail,
    },
    { status: 503 },
  );
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (!isApiPath(url.pathname)) {
      return env.ASSETS.fetch(request);
    }

    const configuredOrigin = (env.ROUTELLM_API_ORIGIN || "").trim().replace(/\/+$/, "");
    if (!configuredOrigin) {
      return apiMisconfigured(
        "Set ROUTELLM_API_ORIGIN to the HTTPS origin of the RouteLLM FastAPI backend.",
      );
    }

    let target;
    try {
      target = new URL(configuredOrigin);
      if (target.protocol !== "https:" && target.protocol !== "http:") {
        throw new Error("unsupported protocol");
      }
    } catch {
      return apiMisconfigured("ROUTELLM_API_ORIGIN must be a valid http(s) origin.");
    }

    target.pathname = url.pathname;
    target.search = url.search;

    const proxyRequest = new Request(target.toString(), request);
    proxyRequest.headers.delete("host");
    proxyRequest.headers.set("X-Forwarded-Proto", url.protocol.replace(":", ""));
    proxyRequest.headers.set("X-Forwarded-Host", url.host);

    const clientIp = request.headers.get("CF-Connecting-IP");
    if (clientIp) {
      proxyRequest.headers.set("X-Forwarded-For", clientIp);
    }

    try {
      return await fetch(proxyRequest);
    } catch {
      return Response.json(
        {
          error: "routellm_backend_unreachable",
          detail: "The configured RouteLLM backend origin could not be reached.",
        },
        { status: 502 },
      );
    }
  },
};
