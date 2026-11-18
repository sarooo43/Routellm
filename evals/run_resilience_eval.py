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
provider attempts, mean latency, and added latency (router minus naive), plus
FIRST-request vs steady-state attempts/latency per arm (T4g).

Modes:
    mocked (default) - deterministic, zero network, runs in CI. NEVER writes
                       to docs/: mocked output goes to the console only.
    --live           - fires the same N requests at a RUNNING backend over
                       HTTP and injects a real Chaos Mode outage. Off by
                       default; provider_down is the only scenario it runs
                       (the other failure classes need provider-side fault
                       injection that would burn real quota/keys). Requires
                       an explicit --yes-spend-quota, aborts if fewer than
                       2 providers have saved keys or the backend does not
                       report CHAOS_MODE_ENABLED, and prints the request
                       budget + providers first (T4h). A live run (and only
                       a live run) writes docs/eval-results.md with a
                       "MODE: LIVE" header (T4c).

Run:
    python evals/run_resilience_eval.py            # mocked, tables
    python evals/run_resilience_eval.py --json     # mocked, machine blob
    python evals/run_resilience_eval.py --live --yes-spend-quota \
        --base-url http://127.0.0.1:8000           # LIVE (spends quota)
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
    restore_structlog,
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
    # T4g: FIRST request vs STEADY STATE. The first request pays one-shot
    # costs the rest of the run does not: the circuit breaker has no failure
    # history yet (so it cannot short-circuit to the secondary) and the
    # denied-model memory is empty. Reporting a single run mean (e.g.
    # "1.02 attempts") mixes those two regimes and hides both.
    first_attempts = attempts[0] if attempts else 0
    first_latency = latencies[0] if latencies else 0.0
    steady_attempts = attempts[1:]
    steady_latencies = latencies[1:]
    return {
        "arm": arm,
        "n": n,
        "successes": successes,
        "success_rate": round(successes / n, 4) if n else 0.0,
        "wilson_95": [round(lo, 4), round(hi, 4)],
        "mean_attempts": round(mean(attempts), 3),
        "mean_latency_ms": round(mean(latencies), 1),
        "first_request": {
            "attempts": first_attempts,
            "latency_ms": round(first_latency, 1),
        },
        "steady_state": {
            "mean_attempts": round(mean(steady_attempts), 3),
            "mean_latency_ms": round(mean(steady_latencies), 1),
        },
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
# Live mode (off by default): a RUNNING backend + Chaos Mode + explicit consent
# ---------------------------------------------------------------------------

DOC_PATH = (
    EVALS_DIR.parent / "docs" / "eval-results.md"
)  # LIVE runs only; mocked runs never write here (T4c)


def git_commit_hash() -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(EVALS_DIR.parent),
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()[:12]
    except Exception:
        return "unknown"


async def preflight_live(client, n: int) -> str:
    """Every gate a live run must pass BEFORE any real request is sent.

    Each refusal exits with a clear, actionable message (T4h). The gates:
      1. fewer than 2 providers have saved keys -> abort (the scenario needs
         a failing primary AND a healthy fallback);
      2. the backend does not report Chaos Mode enabled -> abort;
      3. otherwise: print the total request budget and which providers may
         be called, and hand back the freshly generated eval API key.
    """
    resp = await client.post("/api/v1/keys/generate", json={"label": "eval-resilience"})
    if resp.status_code != 200:
        raise SystemExit(f"FATAL: key generation failed (HTTP {resp.status_code}).")
    api_key = resp.json()["api_key"]

    resp = await client.get("/api/v1/provider-keys", headers={"X-API-Key": api_key})
    if resp.status_code != 200:
        raise SystemExit(
            f"FATAL: provider-key status check failed (HTTP {resp.status_code})."
        )
    configured = [
        row["provider_name"] for row in resp.json() if row.get("configured")
    ]
    if len(configured) < 2:
        raise SystemExit(
            "FATAL: refusing to run --live: fewer than 2 providers have saved "
            f"keys (configured: {configured or 'none'}). The provider_down "
            "scenario needs a failing primary AND a healthy fallback - save "
            "provider keys in the dashboard first."
        )

    resp = await client.get("/api/v1/admin/chaos/status")
    if resp.status_code == 404:
        raise SystemExit(
            "FATAL: refusing to run Chaos Mode: the backend reports it is NOT "
            "enabled (the /api/v1/admin/chaos/* endpoints answer 404 when "
            "CHAOS_MODE_ENABLED is false). Set CHAOS_MODE_ENABLED=true in "
            "backend/.env and restart the backend."
        )
    if resp.status_code != 200:
        raise SystemExit(
            f"FATAL: chaos status check failed (HTTP {resp.status_code})."
        )

    budget = n * 1  # live mode fires exactly ONE scenario (provider_down)
    print("MODE: LIVE - this run calls REAL providers and spends REAL quota.")
    print(
        f"Request budget: {n} requests x 1 scenario (provider_down) "
        f"= {budget} /api/v1/route calls total."
    )
    print(f"Providers that may be called: {', '.join(configured)}")
    return api_key


async def run_scenario_live(client, api_key: str, n: int) -> dict:
    """provider_down against a real backend: chaos-inject an outage on the
    primary provider, fire n route requests through the router arm, clear
    chaos. Uses the httpx client passed by the caller; prints nothing
    sensitive."""
    spec = SCENARIOS["provider_down"]
    # 1. chaos inject on the primary (CHAOS_MODE_ENABLED verified in preflight)
    resp = await client.post(
        "/api/v1/admin/chaos/inject",
        json={"provider_name": PRIMARY, "duration_seconds": 120},
    )
    if resp.status_code != 200:
        raise SystemExit(f"FATAL: chaos inject failed (HTTP {resp.status_code}).")

    successes = 0
    latencies: list[float] = []
    attempts: list[int] = []
    models_called: set[str] = set()
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
            chain = body.get("fallback_chain_attempted") or []
            attempts.append(len(chain))
            if body.get("model_used"):
                models_called.add(str(body["model_used"]))
            for attempt in chain:
                if isinstance(attempt, dict) and attempt.get("provider_name"):
                    models_called.add(str(attempt["provider_name"]))
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
        "models_called": sorted(models_called),
        "note": "live mode runs the router arm only; no naive baseline is fired at a real backend",
    }


def write_live_doc(result: dict, n: int, base_url: str) -> None:
    """A live run (and only a live run) writes docs/eval-results.md (T4c)."""
    from datetime import datetime, timezone

    header = "\n".join(
        [
            "# Eval results (LIVE)",
            "",
            "> **MODE: LIVE — the resilience section below was measured against "
            "> REAL providers with real keys. Everything else in this repo's "
            "> docs stays unmeasured until you run it for real.**",
            "",
            f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
            f"- Commit: `{git_commit_hash()}`",
            f"- Backend: {base_url}",
            f"- Requests: {n} (provider_down scenario, router arm only)",
            f"- Providers/models actually called: "
            f"{', '.join(result.get('models_called') or []) or '(none recorded)'}",
            "",
            "---",
            "",
        ]
    )
    DOC_PATH.parent.mkdir(parents=True, exist_ok=True)
    DOC_PATH.write_text(
        header + render_markdown([result], n, "live (real backend, chaos-injected outage)")
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote {DOC_PATH.relative_to(DOC_PATH.parents[2])}")


def render_markdown(results: list[dict], n: int, mode: str) -> str:
    lines = [
        "## Resilience (E2)",
        "",
        f"Mode: **{mode}**. {n} requests per scenario/arm through the real "
        "fallback router vs a naive single-provider baseline "
        f"(`force_provider={PRIMARY}`). Success rates carry Wilson 95% intervals.",
        "",
        "**The naive arm fails by construction when its only provider is "
        "down** — it hardcodes exactly one provider and has no fallback, so "
        "in every provider-down scenario its failure is a property of the "
        "arm's definition, not an observation about real provider "
        "reliability.",
        "",
        "**What this comparison shows / does not show:** it demonstrates "
        "that FAILOVER WORKS — that the router's fallback chain, circuit "
        "breaker and chaos controller recover a request when the primary "
        "fails. It does NOT show how often real providers fail, how often "
        "you would need that failover in production, or anything about real "
        "provider error semantics; the failures here are scripted at the "
        "transport boundary.",
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
            suffix = " ¹" if arm_key == "naive" else ""
            lines.append(
                f"| {r['scenario']} | {label}{suffix} | {arm['success_rate'] * 100:.1f}% "
                f"({arm['successes']}/{arm['n']}) | [{lo * 100:.1f}%, {hi * 100:.1f}%] "
                f"| {arm['mean_attempts']:.2f} | {arm['mean_latency_ms']:.0f} |"
            )
    lines += [
        "",
        "¹ naive: fails by construction when its only provider is down "
        f"(`force_provider={PRIMARY}`, `max_fallbacks=1`).",
        "",
        "Added latency (router minus naive, mean ms per request):",
        "",
    ]
    for r in results:
        if r.get("added_latency_ms") is not None:
            lines.append(f"- `{r['scenario']}`: {r['added_latency_ms']:.0f} ms")
    lines += [
        "",
        "### First request vs steady state (T4g)",
        "",
        "| Scenario | Arm | First req attempts | First req latency (ms) | Steady mean attempts | Steady mean latency (ms) |",
        "|---|---|---|---|---|---|",
    ]
    for r in results:
        for arm_key, label in (("router", "router"), ("naive", "naive")):
            arm = r.get(arm_key)
            if arm is None:
                continue
            first, steady = arm["first_request"], arm["steady_state"]
            lines.append(
                f"| {r['scenario']} | {label} | {first['attempts']:.2f} "
                f"| {first['latency_ms']:.0f} | {steady['mean_attempts']:.2f} "
                f"| {steady['mean_latency_ms']:.0f} |"
            )
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
    # T4j/CI-regression fix: EVERY exit path must restore the app's logging,
    # including the early SystemExit refusals below (--live without
    # --yes-spend-quota, preflight failures). A leaked silence broke later
    # stdout-capturing tests under pytest (test_generated_key_is_never_logged
    # captured nothing). This also matters because pytest loads this script
    # via importlib under a different module name than harness.py itself, so
    # the test suite's restore fixture cannot see this module's globals.
    try:
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
        parser.add_argument(
            "--yes-spend-quota",
            action="store_true",
            help="REQUIRED with --live: explicit consent that real provider "
            "quota will be spent. --live without this flag refuses to run.",
        )
        parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="backend URL for --live")
        args = parser.parse_args()

        if args.live and not args.yes_spend_quota:
            raise SystemExit(
                "Refusing to run --live without --yes-spend-quota: live mode "
                "calls real providers and spends real quota. If that is intended, "
                "re-run with: --live --yes-spend-quota"
            )

        if args.live:
            import httpx

            async def _live() -> dict:
                async with httpx.AsyncClient(
                    base_url=args.base_url, timeout=30.0
                ) as client:
                    api_key = await preflight_live(client, args.n)
                    return await run_scenario_live(client, api_key, args.n)

            result = asyncio.run(_live())
            results = [result]
            mode = "live (real backend, chaos-injected outage)"
            write_live_doc(result, args.n, args.base_url)
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
    finally:
        restore_structlog()


if __name__ == "__main__":
    main()
