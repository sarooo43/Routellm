#!/usr/bin/env python3
"""Post-deploy smoke test for a live RouteLLM deployment.

Works against a Hugging Face Space, a self-hosted instance, or a local server.
Pure stdlib + httpx, so it runs the same on Windows PowerShell, macOS and Linux.

Usage::

    python scripts/smoke_deployed.py --api https://<owner>-<space>.hf.space \\
                                     --origin https://routellm.saboornasar66.workers.dev

Exit code 0 only when every *critical* check passes; a clean business failure
(no provider keys configured, quota exhausted) is reported but not fatal.

Secrets: the generated demo key is masked everywhere (last 4 chars only) and is
never written to disk.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

try:
    import httpx
except ImportError:  # pragma: no cover - guidance beats a traceback
    sys.exit("httpx is required: pip install httpx")

WAKE_RETRIES = 8
WAKE_SLEEP_S = 10
TIMEOUT_S = 30.0

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    critical: bool = True

    def render(self) -> str:
        tag = f"{GREEN}PASS{RESET}" if self.ok else (f"{RED}FAIL{RESET}" if self.critical else f"{YELLOW}WARN{RESET}")
        suffix = f" {DIM}({self.detail}){RESET}" if self.detail else ""
        return f"[{tag}] {self.name}{suffix}"


def mask(key: str) -> str:
    """Show only the last 4 characters of a key."""
    if not key:
        return "<empty>"
    return f"****{key[-4:]}" if len(key) > 4 else "****"


def wait_for_wake(client: httpx.Client, base: str) -> Result:
    """A sleeping Space returns 503/524 or refuses connections for ~30-60s."""
    last = ""
    for attempt in range(1, WAKE_RETRIES + 1):
        try:
            resp = client.get(f"{base}/health")
            if resp.status_code == 200:
                body = resp.json()
                detail = f"status={body.get('status')} db={body.get('db')} redis={body.get('redis')}"
                return Result("GET /health", True, detail)
            last = f"HTTP {resp.status_code}"
        except httpx.HTTPError as exc:
            last = type(exc).__name__
        if attempt < WAKE_RETRIES:
            print(f"  waking up... ({attempt}/{WAKE_RETRIES}, last: {last})")
            time.sleep(WAKE_SLEEP_S)
    return Result("GET /health", False, f"never became healthy ({last})")


def check_json(client: httpx.Client, base: str, path: str, name: str, expect_key: str | None = None) -> Result:
    try:
        resp = client.get(f"{base}{path}")
    except httpx.HTTPError as exc:
        return Result(name, False, f"{type(exc).__name__}: {exc}")
    if resp.status_code != 200:
        return Result(name, False, f"HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        return Result(name, False, "response was not JSON")
    if expect_key is not None and expect_key not in body:
        return Result(name, False, f"missing '{expect_key}'")
    detail = ""
    if isinstance(body, dict) and "models" in body:
        detail = f"{len(body['models'])} models"
    elif isinstance(body, dict) and "providers" in body:
        detail = f"{len(body['providers'])} providers, live={body.get('live_stats_available')}"
    elif isinstance(body, dict) and "data" in body:
        detail = f"{len(body['data'])} models"
    return Result(name, True, detail)


def check_cors_preflight(client: httpx.Client, base: str, origin: str) -> Result:
    try:
        resp = client.options(
            f"{base}/api/v1/route",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-api-key,content-type",
            },
        )
    except httpx.HTTPError as exc:
        return Result("CORS preflight", False, f"{type(exc).__name__}: {exc}")
    allowed = resp.headers.get("access-control-allow-origin")
    if allowed != origin:
        return Result(
            "CORS preflight",
            False,
            f"allow-origin={allowed!r} expected {origin!r} — add it to CORS_ORIGINS",
        )
    allow_headers = resp.headers.get("access-control-allow-headers", "").lower()
    if "x-api-key" not in allow_headers:
        return Result("CORS preflight", False, f"x-api-key not allowed ({allow_headers!r})")
    return Result("CORS preflight", True, f"origin={origin}")


def check_root(client: httpx.Client, base: str) -> Result:
    try:
        resp = client.get(f"{base}/", follow_redirects=False)
    except httpx.HTTPError as exc:
        return Result("GET /", False, f"{type(exc).__name__}: {exc}")
    if resp.status_code in (200, 307, 308):
        return Result("GET /", True, f"HTTP {resp.status_code}")
    return Result("GET /", False, f"HTTP {resp.status_code}")


def check_routing(client: httpx.Client, base: str) -> list[Result]:
    """Generate a key, then route one tiny prompt through it."""
    results: list[Result] = []
    try:
        gen = client.post(f"{base}/api/v1/keys/generate", json={"label": "smoke-test"})
    except httpx.HTTPError as exc:
        return [Result("POST /api/v1/keys/generate", False, f"{type(exc).__name__}: {exc}")]

    if gen.status_code == 404:
        return [Result("POST /api/v1/keys/generate", False, "endpoint not present", critical=False)]
    if gen.status_code != 201:
        return [Result("POST /api/v1/keys/generate", False, f"HTTP {gen.status_code} {gen.text[:120]}")]

    api_key = gen.json().get("api_key", "")
    results.append(Result("POST /api/v1/keys/generate", bool(api_key), f"key {mask(api_key)}"))
    if not api_key:
        return results

    try:
        route = client.post(
            f"{base}/api/v1/route",
            headers={"X-API-Key": api_key},
            json={"prompt": "Reply with the single word: pong", "max_fallbacks": 1},
        )
    except httpx.HTTPError as exc:
        results.append(Result("POST /api/v1/route", False, f"{type(exc).__name__}: {exc}"))
        return results

    if route.status_code >= 500:
        results.append(Result("POST /api/v1/route", False, f"HTTP {route.status_code} {route.text[:160]}"))
        return results
    try:
        body = route.json()
    except ValueError:
        results.append(Result("POST /api/v1/route", False, f"non-JSON HTTP {route.status_code}"))
        return results

    # A clean "all providers failed" is expected on a public Space with no
    # provider keys configured: the gateway still behaved correctly.
    if body.get("success"):
        results.append(
            Result("POST /api/v1/route", True, f"routed to {body.get('model_used')}")
        )
    else:
        results.append(
            Result(
                "POST /api/v1/route",
                True,
                f"clean business failure: {str(body.get('error'))[:80]}",
            )
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test a deployed RouteLLM API.")
    parser.add_argument("--api", required=True, help="Base URL, e.g. https://x-y.hf.space")
    parser.add_argument(
        "--origin",
        default="",
        help="Frontend origin for the CORS check, e.g. https://routellm.example.workers.dev",
    )
    parser.add_argument(
        "--skip-routing",
        action="store_true",
        help="Skip the key-generation + routing checks (read-only verification).",
    )
    args = parser.parse_args(argv)

    base = args.api.rstrip("/")
    print(f"Smoke testing {base}")
    results: list[Result] = []

    with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as client:
        results.append(wait_for_wake(client, base))
        if not results[0].ok:
            print(results[0].render())
            print(f"\n{RED}The API never became reachable — not running the rest.{RESET}")
            return 1

        results.append(check_json(client, base, "/api/v1/models", "GET /api/v1/models", "providers"))
        results.append(check_json(client, base, "/api/v1/catalog", "GET /api/v1/catalog", "models"))
        results.append(check_json(client, base, "/v1/models", "GET /v1/models", "data"))
        results.append(check_root(client, base))

        if args.origin:
            results.append(check_cors_preflight(client, base, args.origin))
        else:
            results.append(
                Result("CORS preflight", True, "skipped (no --origin given)", critical=False)
            )

        if not args.skip_routing:
            results.extend(check_routing(client, base))

    print()
    for result in results:
        print(result.render())

    failed = [r for r in results if not r.ok and r.critical]
    print()
    if failed:
        print(f"{RED}{len(failed)} critical check(s) failed.{RESET}")
        return 1
    print(f"{GREEN}All critical checks passed.{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
