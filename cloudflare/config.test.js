import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { unstable_readConfig } from "wrangler";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const config = unstable_readConfig(
  { config: path.join(root, "wrangler.jsonc") },
  { hideWarnings: true },
);

test("Workers config pins a stable, non-future compatibility date", () => {
  // Keep this intentionally older than the deployment date. Cloudflare
  // validates compatibility dates in UTC; using "today" caused error 10021.
  assert.equal(config.compatibility_date, "2025-09-01");
  assert.ok(config.compatibility_date <= new Date().toISOString().slice(0, 10));
});

test("Workers config keeps the dashboard assets and API routing contract", () => {
  assert.equal(config.name, "routellm");
  assert.ok(config.main.endsWith(`${path.sep}cloudflare${path.sep}worker.js`));
  assert.equal(config.assets.binding, "ASSETS");
  assert.equal(config.assets.directory, "./frontend/dist");
  assert.equal(config.assets.not_found_handling, "single-page-application");
  assert.deepEqual(config.assets.run_worker_first, [
    "/api/*",
    "/v1/*",
    "/health",
    "/docs",
    "/docs/*",
    "/openapi.json",
    "/redoc",
  ]);
});

test("BACKEND_URL is a plain, non-secret runtime var pointing at the Space", () => {
  // Problem 2 architecture: the Worker proxies same-origin API paths to the
  // HF Space named here. It MUST remain a plain var (not a secret) and hold
  // no credentials — it is the public Space URL.
  assert.ok(config.vars, "vars block missing");
  const raw = fs
    .readFileSync(path.join(root, "wrangler.jsonc"), "utf8")
    .split("\n")
    // Strip full-line // comments (string values like https://… must survive)
    .filter((line) => !/^\s*\/\//.test(line))
    .join("\n");
  const parsed = JSON.parse(raw.trim());
  const backendUrl = parsed.vars?.BACKEND_URL;
  assert.equal(typeof backendUrl, "string");
  assert.match(backendUrl, /^https:\/\//);
  assert.ok(!/token|key|secret|password|Bearer/i.test(backendUrl));
  assert.ok(backendUrl.includes(".hf.space"), "backend points at the HF Space");
  assert.equal(config.vars.BACKEND_URL, backendUrl);
});

test("Workers observability matches the Cloudflare dashboard snapshot", () => {
  assert.deepEqual(config.observability, {
    enabled: false,
    head_sampling_rate: 1,
    redact_query_string: false,
    logs: {
      enabled: true,
      head_sampling_rate: 1,
      invocation_logs: true,
      persist: true,
    },
    traces: {
      enabled: true,
      head_sampling_rate: 1,
      persist: true,
    },
    issues: {
      enabled: true,
    },
  });
});

test("deploy script explicitly uses repo config and preserves dashboard vars", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(root, "package.json"), "utf8"));
  assert.match(pkg.scripts.deploy, /--config wrangler\.jsonc/);
  assert.match(pkg.scripts.deploy, /--keep-vars/);
  assert.match(pkg.scripts.deploy, /--no-autoconfig/);
  assert.doesNotMatch(pkg.scripts.deploy, /--latest/);
});
