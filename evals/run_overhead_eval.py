"""E4 - Overhead eval: what does routing itself cost?

Fires N identical requests (default 200) through the real router twice:
    auto  - full path: classify -> rank -> pre-flight gates -> attempt
    naive - force_provider: straight to one provider, zero routing logic

Both arms hit the SAME scripted provider with the SAME scripted latency,
so every percentile DELTA between the arms is pure routing overhead
(classifier, scorer, breaker/quota pre-flight, DB logging) and not
provider time. Raw p50/p95 latencies are also reported per arm.

MOCKED MODE ONLY in this repo run: the provider is a deterministic stub.
The wall-clock latencies include the scripted provider sleep; only the
deltas isolate the router's own overhead.

Run:
    python evals/run_overhead_eval.py           # human tables
    python evals/run_overhead_eval.py --json    # machine blob
"""

from __future__ import annotations

import argparse
import asyncio
import json
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
    make_scripted_response,
    percentile,
    restore_structlog,
    silence_structlog,
    count_real_attempts,
)
from app.providers.registry import PROVIDER_REGISTRY  # noqa: E402

PROVIDER = "groq"
TASK_TYPE = "qa"
PROBE_PROMPT = "What is the capital of Australia?"
SCRIPTED_LATENCY_MS = 30.0


def install_scripted_provider() -> dict:
    """One healthy scripted provider; everything else removed for the run
    (the local env may carry operator keys - never make a real call)."""

    def behavior(prompt: str, call_index: int):
        return make_scripted_response(
            PROVIDER, f"{PROVIDER}-scripted-model", latency_ms=SCRIPTED_LATENCY_MS
        )

    saved = dict(PROVIDER_REGISTRY)
    PROVIDER_REGISTRY.clear()
    PROVIDER_REGISTRY.update(
        {PROVIDER: ScriptedProvider(PROVIDER, f"{PROVIDER}-scripted-model", behavior)}
    )
    return saved


def restore_registry(saved: dict) -> None:
    PROVIDER_REGISTRY.clear()
    PROVIDER_REGISTRY.update(saved)


async def run_arm(arm: str, n: int) -> dict:
    from app.core.executor import execute_with_fallback

    maker, breaker, quota, chaos = await build_eval_stack()
    latencies: list[float] = []
    attempts: list[int] = []
    async with maker() as session:
        for _ in range(n):
            kwargs = {} if arm == "auto" else {"force_provider": PROVIDER}
            result = await execute_with_fallback(
                PROBE_PROMPT,
                session,
                task_type=TASK_TYPE,
                max_fallbacks=3,
                circuit_breaker=breaker,
                quota_tracker=quota,
                chaos_controller=chaos,
                **kwargs,
            )
            latencies.append(result.total_latency_ms)
            attempts.append(count_real_attempts(result.fallback_chain_attempted))
    ordered = sorted(latencies)
    return {
        "arm": arm,
        "n": n,
        "p50_ms": round(percentile(ordered, 50), 1),
        "p95_ms": round(percentile(ordered, 95), 1),
        "mean_ms": round(sum(ordered) / len(ordered), 1),
        "mean_attempts": round(sum(attempts) / len(attempts), 3),
    }


def run_overhead_eval(n: int) -> dict:
    saved = install_scripted_provider()
    try:
        auto = asyncio.run(run_arm("auto", n))
        naive = asyncio.run(run_arm("naive", n))
    finally:
        restore_registry(saved)
    return {
        "eval": "overhead",
        "mode": "mocked (scripted provider; deltas isolate routing overhead)",
        "scripted_provider_latency_ms": SCRIPTED_LATENCY_MS,
        "auto": auto,
        "naive": naive,
        "overhead_delta": {
            "p50_ms": round(auto["p50_ms"] - naive["p50_ms"], 1),
            "p95_ms": round(auto["p95_ms"] - naive["p95_ms"], 1),
            "mean_ms": round(auto["mean_ms"] - naive["mean_ms"], 1),
        },
    }


def render_markdown(result: dict) -> str:
    auto, naive, d = result["auto"], result["naive"], result["overhead_delta"]
    return "\n".join(
        [
            "## Overhead (E4)",
            "",
            f"Mode: **{result['mode']}** - {auto['n']} identical requests per arm "
            f"through the real router (auto) vs the forced single-provider path "
            f"(naive). Scripted provider latency: {result['scripted_provider_latency_ms']:.0f} ms.",
            "",
            "| Arm | n | p50 (ms) | p95 (ms) | mean (ms) |",
            "|---|---|---|---|---|",
            f"| auto | {auto['n']} | {auto['p50_ms']:.1f} | {auto['p95_ms']:.1f} | {auto['mean_ms']:.1f} |",
            f"| naive | {naive['n']} | {naive['p50_ms']:.1f} | {naive['p95_ms']:.1f} | {naive['mean_ms']:.1f} |",
            "",
            "**Routing overhead delta (auto minus naive)** "
            "- pure router cost (classifier + ranking + pre-flight gates + "
            "attempt logging), provider time cancels out:",
            "",
            f"- p50: {d['p50_ms']:.1f} ms",
            f"- p95: {d['p95_ms']:.1f} ms",
            f"- mean: {d['mean_ms']:.1f} ms",
        ]
    )


def main() -> None:
    silence_structlog()
    try:
        parser = argparse.ArgumentParser(
            description="E4: overhead eval - router p50/p95 latency vs the mock provider."
        )
        parser.add_argument("--n", type=int, default=200, help="requests per arm (default 200)")
        parser.add_argument("--json", action="store_true", help="print the raw result blob")
        args = parser.parse_args()

        result = run_overhead_eval(args.n)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(render_markdown(result))
    finally:
        # Same T4j contract as the resilience eval: never leave the app's
        # logging silenced on any exit path.
        restore_structlog()


if __name__ == "__main__":
    main()
