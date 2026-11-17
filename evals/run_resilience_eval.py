"""E2 - Resilience eval: does the router survive provider failures?

Five failure scenarios, each run N times (default 50) through the REAL
fallback router (``app.core.executor.execute_with_fallback``) and through a
naive single-provider baseline (``force_provider`` - one hardcoded provider,
no fallback, no breaker). Only the provider TRANSPORT is mocked; the router,
circuit breaker, quota tracker and chaos controller are the real ones.

Scenarios (primary = scripted-failing provider, secondary = healthy):
    provider_down      - primary returns network_error immediately
    rate_limited_429   - primary returns rate_limit_exceeded (Retry-After 30s)
    retired_model_404  - primary returns model_not_found (HTTP 404)
    key_wide_403       - primary returns auth_failed (HTTP 403)
    slow_response      - primary stalls 2500ms then errors (simulated timeout)

Reported per scenario/arm: success rate with Wilson 95% interval, mean real
provider attempts, mean latency, and added latency (router minus naive).

Modes:
    mocked (default) - deterministic, zero network, runs in CI.
    --live           - fires the same N requests at a RUNNING backend over
                       HTTP and injects a real Chaos Mode outage. Off by
                       default; provider_down is the only scenario it runs
                       (the other failure classes need provider-side fault
                       injection that would burn real quota/keys).

Run:
    python evals/run_resilience_eval.py            # mocked, tables
    python evals/run_resilience_eval.py --json     # mocked, machine blob
    python evals/run_resilience_eval.py --live --base-url http://127.0.0.1:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVALS_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from harness import (  # noqa: E402
    ScriptedProvider,
    build_eval_stack,
    count_real_attempts,
    mean,
    make_scripted_response,
    silence_structlog,
    wilson_interval,
)
from app.providers.registry import PROVIDER_REGISTRY  # noqa: E402

PRIMARY = "groq"      # ranks first with zero stats for qa -> the scripted failure target
SECONDARY = "deepseek"  # ranks after groq -> the healthy fallback target
TASK_TYPE = "qa"
PROBE_PROMPT = "What is the capital of Australia?"
FAST_MS = 30.0
SLOW_MS = 250.0  # real wall-clock stall in the scripted provider (8x the fast path)

SCENARIOS: dict[str, dict] = {
    "provider_down": {
        "description": "primary returns network_error immediately (connection refused / timeout)",
        "primary": dict(success=False, error="connection refused", outcome="network_error", latency_ms=20.0),
        "live": False,
    },
    "rate_limited_429": {
        "description": "primary returns rate_limit_exceeded with Retry-After: 30",
        "primary": dict(
            success=False,
            error="rate_limit_exceeded",
            outcome="rate_limit_exceeded",
            http_status=429,
            retry_after=30.0,
            latency_ms=15.0,
        ),
        "live": False,
    },
    "retired_model_404": {
        "description": "primary returns model_not_found (HTTP 404, model retired)",
        "primary": dict(
            success=False,
            error="model_not_found",
            outcome="model_not_found",
            http_status=404,
            latency_ms=25.0,
        ),
        "live": False,
    },
    "key_wide_403": {
        "description": "primary returns auth_failed (HTTP 403, whole key revoked)",
        "primary": dict(
            success=False,
            error="auth_failed",
            outcome="auth_failed",
            http_status=403,
            latency_ms=25.0,
        ),
        "live": False,
    },
    "slow_response": {
        "description": "primary stalls 250ms (real sleep) then errors (simulated timeout)",
        "primary": dict(
            success=False,
            error="simulated timeout",
            outcome="network_error",
            latency_ms=SLOW_MS,
        ),
        "live": False,
    },
}


def install_scripted_providers(scenario_name: str) -> dict:
    """Patch the real registry with deterministic stubs for one scenario.

    Every OTHER provider is temporarily removed, not just overridden: the
    local environment may carry operator keys, and the router would happily
    make a REAL call to a real provider (burning quota and breaking
    determinism). Only the scripted pair is routable during the eval.
    """
    spec = SCENARIOS[scenario_name]

    def primary_behavior(prompt: str, call_index: int) -> "object":
        return make_scripted_response(
            PRIMARY, f"{PRIMARY}-scripted-model", **spec["primary"]
        )

    def secondary_behavior(prompt: str, call_index: int) -> "object":
        return make_scripted_response(
            SECONDARY, f"{SECONDARY}-scripted-model", latency_ms=FAST_MS
        )

    stubs = {
        PRIMARY: ScriptedProvider(PRIMARY, f"{PRIMARY}-scripted-model", primary_behavior),
        SECONDARY: ScriptedProvider(SECONDARY, f"{SECONDARY}-scripted-model", secondary_behavior),
    }
    saved = dict(PROVIDER_REGISTRY)
    PROVIDER_REGISTRY.clear()
    PROVIDER_REGISTRY.update(stubs)
    return saved


def restore_registry(saved: dict) -> None:
    PROVIDER_REGISTRY.clear()
    PROVIDER_REGISTRY.update(saved)


async def run_arm(
    scenario_name: str,
    arm: str,
    n: int,
    max_fallbacks: int = 3,
) -> dict:
    """Run n identical requests through one arm on a fresh isolated stack."""
    from app.core.executor import execute_with_fallback

    maker, breaker, quota, chaos = await build_eval_stack()
    async with maker() as session:
        latencies: list[float] = []
        attempts: list[int] = []
        successes = 0
        for _ in range(n):
            if arm == "router":
                result = await execute_with_fallback(
                    PROBE_PROMPT,
                    session,
                    task_type=TASK_TYPE,
                    max_fallbacks=max_fallbacks,
                    circuit_breaker=breaker,
                    quota_tracker=quota,
                    chaos_controller=chaos,
                )
                chain = result.fallback_chain_attempted
            else:  # naive: one hardcoded provider, zero fallback, no gates
                result = await execute_with_fallback(
                    PROBE_PROMPT,
                    session,
                    task_type=TASK_TYPE,
                    force_provider=PRIMARY,
                )
                chain = result.fallback_chain_attempted
            successes += 1 if result.success else 0
            latencies.append(result.total_latency_ms)
            attempts.append(count_real_attempts(chain))
    lo, hi = wilson_interval(successes, n)
    return {
        "arm": arm,
        "n": n,
        "successes": successes,
        "success_rate": round(successes / n, 4) if n else 0.0,
        "wilson_95": [round(lo, 4), round(hi, 4)],
        "mean_attempts": round(mean(attempts), 3),
        "mean_latency_ms": round(mean(latencies), 1),
    }


def run_scenario_mocked(scenario_name: str, n: int, max_fallbacks: int) -> dict:
    """One scenario end-to-end (fresh stacks, deterministic, no network)."""
    saved = install_scripted_providers(scenario_name)
    try:
        router = asyncio.run(run_arm(scenario_name, "router", n, max_fallbacks))
        naive = asyncio.run(run_arm(scenario_name, "naive", n, max_fallbacks))
    finally:
        restore_registry(saved)
    return {
        "scenario": scenario_name,
        "description": SCENARIOS[scenario_name]["description"],
        "router": router,
        "naive": naive,
        "added_latency_ms": round(router["mean_latency_ms"] - naive["mean_latency_ms"], 1),
    }


# ---------------------------------------------------------------------------
# Live mode (off by default): a RUNNING backend + Chaos Mode
# ---------------------------------------------------------------------------


async def run_scenario_live(base_url: str, n: int) -> dict:
    """provider_down against a real backend: chaos-inject an outage on the
    primary provider, fire n route requests through the router arm, clear
    chaos. Uses httpx only; prints nothing sensitive."""
    import httpx

    spec = SCENARIOS["provider_down"]
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        # 1. a real API key for the route endpoint
        resp = await client.post("/api/v1/keys/generate", json={"label": "eval-resilience"})
        if resp.status_code != 200:
            raise SystemExit(f"FATAL: key generation failed (HTTP {resp.status_code}).")
        api_key = resp.json()["api_key"]

        # 2. chaos inject on the primary (CHAOS_MODE_ENABLED must be on server-side)
        resp = await client.post(
            "/api/v1/admin/chaos/inject",
            json={"provider_name": PRIMARY, "duration_seconds": 120},
        )
        if resp.status_code == 404:
            raise SystemExit(
                "FATAL: chaos endpoints 404 - start the backend with "
                "CHAOS_MODE_ENABLED=true to run --live."
            )
        if resp.status_code != 200:
            raise SystemExit(f"FATAL: chaos inject failed (HTTP {resp.status_code}).")

        successes = 0
        latencies: list[float] = []
        attempts: list[int] = []
        try:
            for _ in range(n):
                route = await client.post(
                    "/api/v1/route",
                    headers={"X-API-Key": api_key},
                    json={"prompt": PROBE_PROMPT, "task_type": TASK_TYPE, "max_fallbacks": 3},
                )
                if route.status_code == 429:
                    raise SystemExit("FATAL: backend rate-limited the eval; raise its limit.")
                route.raise_for_status()
                body = route.json()
                successes += 1 if body.get("success") else 0
                latencies.append(float(body.get("total_latency_ms") or 0.0))
                attempts.append(len(body.get("fallback_chain_attempted") or []))
        finally:
            await client.post(
                "/api/v1/admin/chaos/clear", json={"provider_name": PRIMARY}
            )

    lo, hi = wilson_interval(successes, n)
    return {
        "scenario": "provider_down",
        "description": spec["description"] + " (LIVE: chaos-injected on real backend)",
        "router": {
            "arm": "router",
            "n": n,
            "successes": successes,
            "success_rate": round(successes / n, 4) if n else 0.0,
            "wilson_95": [round(lo, 4), round(hi, 4)],
            "mean_attempts": round(mean(attempts), 3),
            "mean_latency_ms": round(mean(latencies), 1),
        },
        "naive": None,
        "added_latency_ms": None,
        "note": "live mode runs the router arm only; no naive baseline is fired at a real backend",
    }


def render_markdown(results: list[dict], n: int, mode: str) -> str:
    lines = [
        "## Resilience (E2)",
        "",
        f"Mode: **{mode}**. {n} requests per scenario/arm through the real "
        "fallback router vs a naive single-provider baseline "
        f"(`force_provider={PRIMARY}`). Success rates carry Wilson 95% intervals.",
        "",
        "| Scenario | Arm | Success rate | Wilson 95% | Mean attempts | Mean latency (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        for arm_key, label in (("router", "router"), ("naive", "naive")):
            arm = r.get(arm_key)
            if arm is None:
                lines.append(f"| {r['scenario']} | {label} | (live mode: not run) | - | - | - |")
                continue
            lo, hi = arm["wilson_95"]
            lines.append(
                f"| {r['scenario']} | {label} | {arm['success_rate'] * 100:.1f}% "
                f"({arm['successes']}/{arm['n']}) | [{lo * 100:.1f}%, {hi * 100:.1f}%] "
                f"| {arm['mean_attempts']:.2f} | {arm['mean_latency_ms']:.0f} |"
            )
    lines += ["", "Added latency (router minus naive, mean ms per request):", ""]
    for r in results:
        if r.get("added_latency_ms") is not None:
            lines.append(f"- `{r['scenario']}`: {r['added_latency_ms']:.0f} ms")
    lines += [
        "",
        "Notes: attempts count REAL provider calls only - pre-flight skips "
        "(`quota_exceeded`, `circuit_open`) are logged but are not calls. The "
        "circuit breaker legitimately trips during a run (repeated failures on "
        "the primary), so later requests skip straight to the healthy "
        "secondary; that appears as a falling mean-attempt count, not as "
        "failures.",
    ]
    return "\n".join(lines)


def main() -> None:
    # The scenarios intentionally produce a flood of expected failure events
    # ("forced_provider_failed_no_fallback", ...) - silence the app's logging
    # so the report is the only thing on stdout.
    silence_structlog()

    parser = argparse.ArgumentParser(
        description="E2: resilience eval - router vs naive single-provider baseline."
    )
    parser.add_argument("--n", type=int, default=50, help="requests per scenario/arm (default 50)")
    parser.add_argument("--max-fallbacks", type=int, default=3)
    parser.add_argument("--json", action="store_true", help="print the raw result blob")
    parser.add_argument(
        "--live",
        action="store_true",
        help="run provider_down against a RUNNING backend with Chaos Mode (off by default)",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="backend URL for --live")
    args = parser.parse_args()

    if args.live:
        result = asyncio.run(run_scenario_live(args.base_url, args.n))
        results = [result]
        mode = "live (real backend, chaos-injected outage)"
    else:
        results = []
        for scenario in SCENARIOS:
            results.append(run_scenario_mocked(scenario, args.n, args.max_fallbacks))
        mode = "mocked (deterministic, no network)"

    blob = {"eval": "resilience", "mode": mode, "n_per_scenario": args.n, "scenarios": results}
    if args.json:
        print(json.dumps(blob, indent=2))
    else:
        print(render_markdown(results, args.n, mode))


if __name__ == "__main__":
    main()
