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
  // Content negotiation: a browser navigating to /health should SEE the fix,
  // while the dashboard (fetch/XHR, Accept: application/json) keeps the
  // structured shape its error handling expects.
  const body = {
    error: "routellm_backend_not_configured",
    detail,
    fix: "Cloudflare dashboard -> Workers & Pages -> routellm -> Settings -> Variables and Secrets -> add ROUTELLM_API_ORIGIN = https://<space>.hf.space, then Deploy. Or commit it to wrangler.jsonc (already done in this repo) and run `npm run deploy`.",
  };
  return Response.json(body, {
    status: 503,
    headers: { "cache-control": "no-store" },
  });
}

/** Human-readable variant for direct browser visits. */
function apiMisconfiguredHtml(detail, fix) {
  return new Response(
    `<!doctype html><meta charset="utf-8"><title>RouteLLM: backend not configured</title>
<style>body{font:16px/1.6 system-ui,sans-serif;max-width:44rem;margin:4rem auto;padding:0 1rem;color:#e8e8ea;background:#111214}
code{background:#1d1f23;padding:.15rem .35rem;border-radius:.25rem}h1{font-size:1.25rem}
.box{border:1px solid #33363c;border-radius:.5rem;padding:1rem;background:#17191d;margin:1rem 0}</style>
<h1>RouteLLM dashboard is up, but its API gateway is not configured</h1>
<p class="box"><strong>${detail}</strong></p>
<p>${fix}</p>
<p style="opacity:.7">This 503 comes from the Cloudflare Worker, not from the FastAPI backend.</p>`,
    { status: 503, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" } },
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
      const detail =
        "ROUTELLM_API_ORIGIN is not set on this Worker version, so /api/*, /v1/* and " +
        "/health cannot be forwarded.";
      if ((request.headers.get("accept") || "").includes("text/html")) {
        return apiMisconfiguredHtml(detail, "Set the variable, then deploy again.");
      }
      return apiMisconfigured(detail);
    }

    let target;
    try {
      target = new URL(configuredOrigin);
      if (target.protocol !== "https:" && target.protocol !== "http:") {
        throw new Error("unsupported protocol");
      }
    } catch {
      const detail = `ROUTELLM_API_ORIGIN must be a valid http(s) origin, got: ${configuredOrigin}`;
      if ((request.headers.get("accept") || "").includes("text/html")) {
        return apiMisconfiguredHtml(detail, "Fix the value, then deploy again.");
      }
      return apiMisconfigured(detail);
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
      const response = await fetch(proxyRequest);
      // Hugging Face can answer a *.hf.space URL with a redirect to the
      // canonical host. A proxy must not forward that: the browser would be
      // sent to the Space's own domain, bypassing the Worker (and the CORS
      // contract). Follow one hop when the target changed, then re-check.
      if (response.status >= 300 && response.status < 400) {
        const location = response.headers.get("location");
        if (location) {
          const next = new URL(location, target);
          if (next.origin !== target.origin) {
            const hop = new Request(next.toString(), request);
            hop.headers.delete("host");
            return await fetch(hop);
          }
        }
      }
      return response;
    } catch {
      // Name the target: "could not be reached" without the host is the least
      // actionable message an operator can get. The origin is a public URL, so
      // echoing it leaks nothing.
      return Response.json(
        {
          error: "routellm_backend_unreachable",
          detail: `The configured RouteLLM backend origin could not be reached: ${target.origin}`,
          origin: target.origin,
          path: url.pathname,
        },
        { status: 502 },
      );
    }
  },
};
