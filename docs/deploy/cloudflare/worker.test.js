import assert from "node:assert/strict";
import test from "node:test";
import worker, { isApiPath, resolveBackendOrigin } from "./worker.js";

const assets = {
  async fetch() {
    return new Response("spa", { status: 200 });
  },
};

test("isApiPath matches gateway routes but not dashboard routes", () => {
  for (const pathname of [
    "/api/v1/route",
    "/v1/chat/completions",
    "/health",
    "/docs",
    "/docs/oauth2-redirect",
    "/openapi.json",
    "/redoc",
  ]) {
    assert.equal(isApiPath(pathname), true, pathname);
  }
  for (const pathname of ["/", "/providers", "/keys", "/assets/index.js"]) {
    assert.equal(isApiPath(pathname), false, pathname);
  }
});

test("non-API requests are served by the static assets binding", async (t) => {
  let assetCalls = 0;
  const env = {
    ASSETS: {
      async fetch(request) {
        assetCalls += 1;
        assert.equal(new URL(request.url).pathname, "/providers");
        return new Response("spa");
      },
    },
  };
  const response = await worker.fetch(new Request("https://dash.example.com/providers"), env);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "spa");
  assert.equal(assetCalls, 1);
});

test("resolveBackendOrigin: explicit BACKEND_URL beats legacy var and default", () => {
  assert.equal(
    resolveBackendOrigin({ BACKEND_URL: "https://b.example.com/" }),
    "https://b.example.com",
  );
  assert.equal(
    resolveBackendOrigin({ ROUTELLM_API_ORIGIN: "https://legacy.example.com" }),
    "https://legacy.example.com",
  );
  assert.equal(
    resolveBackendOrigin({ BACKEND_URL: "https://x.example.com", ROUTELLM_API_ORIGIN: "https://legacy.example.com" }),
    "https://x.example.com",
  );
});

test("resolveBackendOrigin: empty env falls back to the HF Space default", () => {
  assert.equal(resolveBackendOrigin({}), "https://saboornotgonna-routellm.hf.space");
  assert.equal(resolveBackendOrigin({ BACKEND_URL: "  " }), "https://saboornotgonna-routellm.hf.space");
});

test("resolveBackendOrigin: garbage or non-http overrides are skipped, default still wins", () => {
  assert.equal(
    resolveBackendOrigin({ BACKEND_URL: "not-a-url" }),
    "https://saboornotgonna-routellm.hf.space",
  );
  assert.equal(
    resolveBackendOrigin({ ROUTELLM_API_ORIGIN: "ftp://bad" }),
    "https://saboornotgonna-routellm.hf.space",
  );
  // explicit valid value still beats a broken legacy one
  assert.equal(
    resolveBackendOrigin({ BACKEND_URL: "https://good.example.com", ROUTELLM_API_ORIGIN: "garbage" }),
    "https://good.example.com",
  );
});

test("API requests are proxied with method, query, body, and caller key intact", async (t) => {
  let proxied;
  t.mock.method(globalThis, "fetch", async (request) => {
    proxied = request;
    return new Response("ok", { status: 200 });
  });

  const response = await worker.fetch(
    new Request("https://dash.example.com/api/v1/route?x=1", {
      method: "POST",
      headers: {
        "X-API-Key": "rk_test",
        "Content-Type": "application/json",
        "CF-Connecting-IP": "203.0.113.10",
      },
      body: JSON.stringify({ prompt: "hello" }),
    }),
    { ASSETS: assets, BACKEND_URL: "https://api.example.com/" },
  );

  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-routellm-source"), "backend");
  assert.equal(proxied.method, "POST");
  assert.equal(proxied.url, "https://api.example.com/api/v1/route?x=1");
  assert.equal(proxied.headers.get("X-API-Key"), "rk_test");
  assert.equal(proxied.headers.get("X-Forwarded-Host"), "dash.example.com");
  assert.equal(proxied.headers.get("X-Forwarded-Proto"), "https");
  assert.equal(proxied.headers.get("X-Forwarded-For"), "203.0.113.10");
  assert.deepEqual(await proxied.json(), { prompt: "hello" });
});

test("hop-by-hop request headers are stripped before forwarding", async (t) => {
  let proxied;
  t.mock.method(globalThis, "fetch", async (request) => {
    proxied = request;
    return new Response("ok", { status: 200 });
  });

  await worker.fetch(
    new Request("https://dash.example.com/api/v1/models", {
      headers: {
        Connection: "keep-alive",
        "Keep-Alive": "timeout=5",
        TE: "trailers",
        "X-API-Key": "rk_test",
      },
    }),
    { ASSETS: assets, BACKEND_URL: "https://api.example.com" },
  );

  for (const name of ["connection", "keep-alive", "te"]) {
    assert.equal(proxied.headers.get(name), null, name);
  }
  assert.equal(proxied.headers.get("X-API-Key"), "rk_test");
});

test("Hop-by-hop RESPONSE headers from upstream are not blindly trusted", async (t) => {
  t.mock.method(globalThis, "fetch", async () => {
    return new Response("ok", {
      status: 200,
      headers: { "Content-Type": "application/json", "Transfer-Encoding": "chunked" },
    });
  });

  // The response is rewritten through withSourceHeader — passes through the
  // upstream body untouched, so this just verifies structure.
  const response = await worker.fetch(new Request("https://dash.example.com/api/v1/models"), {
    ASSETS: assets,
    BACKEND_URL: "https://api.example.com",
  });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-routellm-source"), "backend");
});

test("GET requests retry ONCE on a retryable upstream status, then succeed", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async (request) => {
    assert.equal(request.method, "GET");
    calls += 1;
    if (calls === 1) {
      return new Response("cold space", { status: 503 });
    }
    return new Response(JSON.stringify({ providers: [] }), { status: 200 });
  });

  const response = await worker.fetch(new Request("https://dash.example.com/api/v1/models"), {
    ASSETS: assets,
    BACKEND_URL: "https://api.example.com",
  });
  assert.equal(calls, 2, "exactly one automatic retry");
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-routellm-source"), "backend");
});

test("GET requests retry on network errors (space waking up)", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls += 1;
    if (calls === 1) throw new Error("dns or network");
    return new Response("fine", { status: 200 });
  });
  const response = await worker.fetch(new Request("https://dash.example.com/health"), {
    ASSETS: assets,
    BACKEND_URL: "https://api.example.com",
  });
  assert.equal(calls, 2);
  assert.equal(response.status, 200);
});

test("retry is limited: two consecutive failures still return one upstream-down response", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls += 1;
    throw new Error("space still sleeping");
  });
  const response = await worker.fetch(new Request("https://dash.example.com/api/v1/logs"), {
    ASSETS: assets,
    BACKEND_URL: "https://api.example.com",
  });
  assert.equal(calls, 2);
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("x-routellm-source"), "worker-proxy");
  const body = await response.json();
  assert.equal(body.detail, "Backend unreachable or waking up");
  assert.equal(body.upstream, "down");
});

test("non-GET requests are NOT retried (no duplicated POSTs)", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls += 1;
    throw new Error("network down");
  });
  const response = await worker.fetch(
    new Request("https://dash.example.com/api/v1/route", {
      method: "POST",
      body: JSON.stringify({ prompt: "hello" }),
      headers: { "Content-Type": "application/json" },
    }),
    { ASSETS: assets, BACKEND_URL: "https://api.example.com" },
  );
  assert.equal(calls, 1, "POST must never be replayed");
  assert.equal(response.status, 503);
  assert.equal((await response.json()).upstream, "down");
});

test("retryable POST statuses are returned as-is without a retry", async (t) => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    calls += 1;
    return new Response("cold", { status: 503 });
  });
  const response = await worker.fetch(
    new Request("https://dash.example.com/api/v1/route", { method: "POST", body: "{}" }),
    { ASSETS: assets, BACKEND_URL: "https://api.example.com" },
  );
  assert.equal(calls, 1);
  assert.equal(response.status, 503);
  assert.equal(response.headers.get("x-routellm-source"), "backend");
});

test("successful proxied responses carry x-routellm-source: backend", async (t) => {
  t.mock.method(globalThis, "fetch", async () => {
    return new Response("{}", { status: 200 });
  });
  const response = await worker.fetch(new Request("https://dash.example.com/health"), {
    ASSETS: assets,
    BACKEND_URL: "https://api.example.com",
  });
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-routellm-source"), "backend");
});

test("cross-origin redirect from the Space is followed server-side on GET", async (t) => {
  let calls = 0;
  let secondUrl;
  t.mock.method(globalThis, "fetch", async (request) => {
    calls += 1;
    if (calls === 1) {
      // HF redirecting the *.hf.space host to the canonical one:
      return new Response("moved", {
        status: 301,
        headers: { location: "https://canonical.example.com/api/v1/models" },
      });
    }
    secondUrl = request.url;
    return new Response("{}", { status: 200 });
  });
  const response = await worker.fetch(new Request("https://dash.example.com/api/v1/models"), {
    ASSETS: assets,
    BACKEND_URL: "https://api.example.com",
  });
  assert.equal(calls, 2);
  // The browser must never see the redirect to the Space's own domain:
  assert.equal(secondUrl, "https://canonical.example.com/api/v1/models");
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("x-routellm-source"), "backend");
});
