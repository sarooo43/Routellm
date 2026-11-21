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
import getpass
import re
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVALS_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from url_validation import SafeArgumentParser, backend_base_url  # noqa: E402

from harness import (  # noqa: E402
    ScriptedProvider,
    build_eval_stack,
    count_real_attempts,
    is_success,
    mean,
    make_scripted_response,
    print_key_lines,
    require_success,
    response_detail,
    restore_structlog,
    silence_structlog,
    wilson_interval,
)
from app.providers.registry import PROVIDER_REGISTRY  # noqa: E402

#: Reuse an existing eval key instead of minting one (see preflight_live).
#: ROUTTENER_ is the current name (the product is Routtener); ROUTELLM_ is the
#: pre-rename name and still works, because a shell profile or CI secret that a
#: user already exported must not silently stop being honoured.
EVAL_KEY_ENV_VAR = "ROUTTENER_EVAL_API_KEY"
LEGACY_EVAL_KEY_ENV_VAR = "ROUTELLM_EVAL_API_KEY"


def eval_key_from_env() -> str:
    """The caller's eval key, or "".

    ROUTTENER_EVAL_API_KEY wins when both are set; the legacy ROUTELLM_ name is
    the fallback. Returns the trimmed value, never logging it.
    """
    for name in (EVAL_KEY_ENV_VAR, LEGACY_EVAL_KEY_ENV_VAR):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""

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


async def revoke_transient_key(client, api_key: str, *, minted: bool) -> None:
    """Delete the key this run minted, so repeated live runs do not accumulate.

    Best-effort by design: revocation happens on an exit path, and failing to
    clean up must never turn a successful eval into a crash. A key we did NOT
    mint (reused from the env) is never touched - it is not ours to delete.
    """
    if not minted or not api_key:
        return
    try:
        resp = await client.delete("/api/v1/keys/me", headers={"X-API-Key": api_key})
    except Exception as exc:  # noqa: BLE001 - cleanup is never fatal
        print(f"note: could not revoke the transient eval key ({type(exc).__name__})")
        return
    if 200 <= resp.status_code < 300:
        print("API key: revoked (the secret was never printed)")
    else:
        print(f"note: could not revoke the transient eval key ({response_detail(resp)})")


class PreflightReport:
    """What preflight found, and what (if anything) blocks a real run.

    Split from the exit decision on purpose: the T0 bug was a *reporting* bug.
    The owner had 3 keys saved and saw "configured: none" with no explanation,
    so a script that dies at the first failed gate is a script that hides its
    own reasoning. --dry-run prints the whole picture and THEN exits nonzero.

    A plain class, not a dataclass: this module is loaded via
    importlib.exec_module without being registered in sys.modules (that is how
    backend/tests/test_evals.py imports it), and @dataclass resolves its own
    module there and raises AttributeError.
    """

    def __init__(
        self,
        api_key: str,
        minted: bool,
        configured: list[str],
        chaos_enabled: bool,
        chaos_detail: str,
        budget: int,
        blocker: str = "",
    ) -> None:
        self.api_key = api_key
        self.minted = minted
        self.configured = configured
        self.chaos_enabled = chaos_enabled
        self.chaos_detail = chaos_detail
        self.budget = budget
        #: Human-readable blocker, or "" when every gate passed.
        self.blocker = blocker

    @property
    def ok(self) -> bool:
        return not self.blocker


def credential_label(api_key: str) -> str:
    """The credential in a form that is safe to print: prefix only."""
    if not api_key:
        return "(none)"
    return f"{api_key[:7]}…"


def _providers_hint(api_key: str, minted: bool) -> str:
    """What to do when the caller has keys saved but detection sees none.

    This is the owner-facing half of the T0 fix. Provider keys are scoped to
    the Routtener key that saved them (``ProviderKeyDB.api_key_id``), so a
    freshly minted key sees an empty list no matter how many keys are saved
    under another one. The old message said "save provider keys in the
    dashboard first" - which the owner had already done, three times.
    """
    lines = [
        "How this was detected:",
        "  GET /api/v1/provider-keys  with header  X-API-Key: "
        f"{credential_label(api_key)}",
        "  The endpoint returns one row per provider; a row counts as",
        "  configured when its \"configured\" field is true.",
        "",
        "If you DO have provider keys saved in the dashboard:",
        "  Provider keys belong to the Routtener API key that saved them.",
        "  Keys saved under a different API key are not visible to this key.",
    ]
    if minted:
        lines += [
            "  This run minted a brand-new key, and a new key owns no",
            "  provider keys - so it sees none, however many you have saved.",
            "",
            "  Do this (all clicks, then one copy-paste):",
            "   1. Dashboard -> API Keys -> Generate demo key. Copy it: that",
            "      is the only time it is shown.",
            "   2. Dashboard -> Provider Keys: with that key active, save your",
            "      Gemini, Groq and DeepSeek keys again.",
            "   3. Run the eval with that key (this is the step printed here):",
            f"        export {EVAL_KEY_ENV_VAR}=<the key you just copied>",
            "        python evals/run_resilience_eval.py --live --dry-run "
            "--yes-spend-quota",
            "  The eval then reuses your key instead of minting a new one,",
            "  and detection sees the providers you saved.",
        ]
    else:
        lines += [
            f"  This run used ${EVAL_KEY_ENV_VAR}, and that key owns no",
            "  provider keys.",
            "",
            "  Do this: open Dashboard -> Provider Keys with THIS key active,",
            "  save your provider keys, and re-run. To use the key that",
            f"  already owns them instead, point {EVAL_KEY_ENV_VAR} at it.",
        ]
    return "\n".join(lines)


async def detect_providers(client, api_key: str) -> list[str]:
    """Which providers the backend says this credential has keys for.

    One endpoint, one credential, one parse - named so a test can call exactly
    what the script calls. Returns [] when the credential simply has none.
    """
    resp = await client.get("/api/v1/provider-keys", headers={"X-API-Key": api_key})
    require_success(resp, "provider-key status check")
    rows = resp.json()
    # Real shape (OBSERVED, ASGITransport + uvicorn): a JSON list of
    # {"provider_name": str, "configured": bool, "key_hint": str|None, ...}
    # covering EVERY registered provider, configured or not.
    return [row["provider_name"] for row in rows if row.get("configured")]


async def collect_preflight(client, n: int, *, supplied_key: str | None = None) -> PreflightReport:
    """Gather every gate's result WITHOUT deciding to exit.

    Never raises for a gate failure; the caller inspects ``blocker``. Raises
    only when a live call itself fails (a dead backend is not a gate result).
    """
    api_key = eval_key_from_env() if supplied_key is None else supplied_key
    minted = not api_key

    if minted:
        resp = await client.post(
            "/api/v1/keys/generate", json={"label": "eval-resilience"}
        )
        require_success(resp, "key generation")
        api_key = resp.json()["api_key"]

    configured = await detect_providers(client, api_key)

    chaos_enabled, chaos_detail = True, ""
    resp = await client.get("/api/v1/admin/chaos/status")
    if resp.status_code == 404:
        chaos_enabled = False
        chaos_detail = (
            "the /api/v1/admin/chaos/* endpoints answer 404 when "
            "CHAOS_MODE_ENABLED is false"
        )
    else:
        require_success(resp, "chaos status check")

    report = PreflightReport(
        api_key=api_key,
        minted=minted,
        configured=configured,
        chaos_enabled=chaos_enabled,
        chaos_detail=chaos_detail,
        budget=n,
    )

    if len(configured) < 2:
        report.blocker = (
            "fewer than 2 providers have saved keys "
            f"(configured: {configured or 'none'}). The provider_down scenario "
            "needs a failing primary AND a healthy fallback."
        )
    elif not chaos_enabled:
        report.blocker = f"Chaos Mode is NOT enabled ({chaos_detail})."

    return report


def print_preflight(report: PreflightReport, base_url: str, *, key_source: str | None = None) -> None:
    """Everything the owner needs to understand the run, pass or fail."""
    if report.minted:
        print_key_lines(report.api_key)
        print("API key: minted for this run, and revoked when the run ends")
        print(f"  hint: export {EVAL_KEY_ENV_VAR}=<key> to reuse a key instead")
    else:
        source = key_source or f"${EVAL_KEY_ENV_VAR}"
        print(f"API key: reused from {source} ({credential_label(report.api_key)})")

    print(
        "Providers detected: "
        f"{', '.join(report.configured) if report.configured else 'none'}"
        f" ({len(report.configured)} with saved keys)"
    )
    print(
        "Chaos check: "
        + ("enabled" if report.chaos_enabled else f"NOT enabled - {report.chaos_detail}")
    )
    print(
        f"Request budget: {report.budget} requests x 1 scenario (provider_down) "
        f"= {report.budget} /api/v1/route calls total."
    )
    if not report.ok:
        print()
        print(_providers_hint(report.api_key, report.minted))


async def preflight_live(client, n: int) -> tuple[str, bool]:
    """Backwards-compatible wrapper: collect, print, and refuse if blocked.

    Kept so existing callers and tests keep one obvious entry point; the
    report-printing split above is what --dry-run needs.
    """
    report = await collect_preflight(client, n)
    print_preflight(report, str(getattr(client, "base_url", "")))
    if not report.ok:
        raise SystemExit(f"FATAL: refusing to run --live: {report.blocker}")
    return report.api_key, report.minted


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
    require_success(resp, "chaos inject")

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
        parser = SafeArgumentParser(
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
        parser.add_argument("--base-url", type=backend_base_url, default="http://127.0.0.1:8000", help="backend URL for --live")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="with --live: run every preflight gate and stop before the "
            "first /api/v1/route call (spends no provider quota). Use it to "
            "prove the backend wiring is right before a real run.",
        )
        parser.add_argument(
            "--api-key-prompt", action="store_true",
            help="Read the Routtener API key privately from the terminal.",
        )
        args = parser.parse_args()

        if args.live and not args.yes_spend_quota:
            raise SystemExit(
                "Refusing to run --live without --yes-spend-quota: live mode "
                "calls real providers and spends real quota. If that is intended, "
                "re-run with: --live --yes-spend-quota"
            )

        if args.live:
            if args.api_key_prompt:
                if not sys.stdin.isatty():
                    raise SystemExit("FATAL: --api-key-prompt requires an interactive terminal.")
                supplied_key = getpass.getpass("Routtener API key: ")
                key_source = "the prompt"
            elif EVAL_KEY_ENV_VAR in os.environ:
                supplied_key = os.environ[EVAL_KEY_ENV_VAR]
                key_source = f"${EVAL_KEY_ENV_VAR}"
            else:
                supplied_key = os.environ.get(LEGACY_EVAL_KEY_ENV_VAR, "")
                key_source = f"${LEGACY_EVAL_KEY_ENV_VAR}"
            if supplied_key and not re.fullmatch(r"rk_[A-Za-z0-9_-]+", supplied_key):
                raise SystemExit("FATAL: the supplied eval API key is malformed; use a Routtener rk_ key without whitespace.")
            if not supplied_key:
                print(
                    "ROUTTENER_EVAL_API_KEY is not set: minting a throwaway key. "
                    "A throwaway key owns no provider keys; set your dashboard key "
                    "for a real run (see docs/running-the-live-eval.md).",
                    flush=True,
                )
            import httpx

            async def _live() -> dict:
                async with httpx.AsyncClient(
                    base_url=args.base_url, timeout=30.0
                ) as client:
                    report = await collect_preflight(client, args.n, supplied_key=supplied_key)
                    print_preflight(report, args.base_url, key_source=key_source)

                    if args.dry_run:
                        # A dry run reports EVERY gate before it decides. The
                        # T0 report was a bare "configured: none" that hid its
                        # own reasoning; printing the whole picture first is
                        # what makes the failure diagnosable.
                        print(
                            "DRY RUN: no /api/v1/route call was sent, so no "
                            "provider quota was spent."
                        )
                        await revoke_transient_key(
                            client, report.api_key, minted=report.minted
                        )
                        if not report.ok:
                            # Exit nonzero only AFTER the full report and the
                            # revoke: an unrevoked key is worse than a clear
                            # failure.
                            raise SystemExit(f"FATAL: {report.blocker}")
                        return {
                            "scenario": "provider_down",
                            "dry_run": True,
                            "requests_sent": 0,
                        }

                    if not report.ok:
                        await revoke_transient_key(
                            client, report.api_key, minted=report.minted
                        )
                        raise SystemExit(f"FATAL: refusing to run --live: {report.blocker}")

                    try:
                        return await run_scenario_live(
                            client, report.api_key, args.n
                        )
                    finally:
                        # Revoke even when the scenario blew up half-way: a
                        # transient key must not outlive the run that made it.
                        await revoke_transient_key(
                            client, report.api_key, minted=report.minted
                        )

            result = asyncio.run(_live())
            results = [result]
            mode = (
                "live (real backend, dry run - no route calls)"
                if args.dry_run
                else "live (real backend, chaos-injected outage)"
            )
            if not args.dry_run:
                # A dry run measured nothing; writing it to docs/eval-results.md
                # would replace real numbers with an empty table.
                write_live_doc(result, args.n, args.base_url)
        else:
            results = []
            for scenario in SCENARIOS:
                results.append(run_scenario_mocked(scenario, args.n, args.max_fallbacks))
            mode = "mocked (deterministic, no network)"

        blob = {"eval": "resilience", "mode": mode, "n_per_scenario": args.n, "scenarios": results}
        if args.json:
            print(json.dumps(blob, indent=2))
        elif args.dry_run and args.live:
            # A dry run measured nothing. Printing the empty results table
            # would look like a failed measurement rather than a rehearsal.
            print(
                "Dry run complete: every gate passed. Re-run without "
                "--dry-run to measure provider_down for real."
            )
        else:
            print(render_markdown(results, args.n, mode))
    finally:
        restore_structlog()


def _run_main_with_a_civil_sigint() -> None:
    """Ctrl-C during a live run must not print a traceback.

    OBSERVED: sending SIGINT mid-run (n=5000, interrupted at ~9 s) printed the
    full asyncio KeyboardInterrupt stack and exited -2. The CLEANUP was correct
    - chaos was cleared and a minted key was revoked by the existing finally
    blocks - but the user sees a crash where they should see one sentence.

    The exception is caught here rather than inside the async code on purpose:
    the ``finally`` blocks that clear chaos and revoke a transient key run as
    the exception unwinds, so catching at the top does not skip them.
    """
    try:
        main()
    except KeyboardInterrupt:
        print(
            "\nInterrupted. Chaos injections were cleared and any key minted for "
            "this run was revoked by the cleanup blocks that just ran.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None


if __name__ == "__main__":
    _run_main_with_a_civil_sigint()
