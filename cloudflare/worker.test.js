import assert from "node:assert/strict";
import test from "node:test";
import worker, { isApiPath } from "./worker.js";

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

test("missing backend origin returns a structured 503, not the SPA", async () => {
  const response = await worker.fetch(
    new Request("https://dash.example.com/api/v1/health"),
    { ASSETS: assets },
  );
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error, "routellm_backend_not_configured");
});

test("invalid backend origin returns a structured 503", async () => {
  const response = await worker.fetch(
    new Request("https://dash.example.com/api/v1/health"),
    { ASSETS: assets, ROUTELLM_API_ORIGIN: "not-a-url" },
  );
  assert.equal(response.status, 503);
  assert.equal((await response.json()).error, "routellm_backend_not_configured");
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
    { ASSETS: assets, ROUTELLM_API_ORIGIN: "https://api.example.com/" },
  );

  assert.equal(response.status, 200);
  assert.equal(proxied.method, "POST");
  assert.equal(proxied.url, "https://api.example.com/api/v1/route?x=1");
  assert.equal(proxied.headers.get("X-API-Key"), "rk_test");
  assert.equal(proxied.headers.get("X-Forwarded-Host"), "dash.example.com");
  assert.equal(proxied.headers.get("X-Forwarded-Proto"), "https");
  assert.equal(proxied.headers.get("X-Forwarded-For"), "203.0.113.10");
  assert.deepEqual(await proxied.json(), { prompt: "hello" });
});

test("unreachable backend returns a structured 502", async (t) => {
  t.mock.method(globalThis, "fetch", async () => {
    throw new Error("network down");
  });
  const response = await worker.fetch(
    new Request("https://dash.example.com/api/v1/health"),
    { ASSETS: assets, ROUTELLM_API_ORIGIN: "https://api.example.com" },
  );
  assert.equal(response.status, 502);
  assert.equal((await response.json()).error, "routellm_backend_unreachable");
});
