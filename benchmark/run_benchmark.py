"""RouteLLM benchmark runner — naive single-model baseline vs full routing.

Standalone async script (NOT part of the FastAPI app). Run it manually
against an ALREADY-RUNNING backend:

    python run_benchmark.py --scenario normal
    python run_benchmark.py --scenario chaos

Each invocation performs TWO arms over the same prompt set:
  1. naive_baseline — POST /api/v1/route with force_provider=<fixed model>
     (gemini by default) and zero fallback: the "hardcoded one model, no
     resilience" alternative.
  2. routellm_full  — POST /api/v1/route with no forcing and default
     max_fallbacks=3: the full classify → rank → fallback pipeline.

Chaos (--scenario chaos) injects a deterministic outage on the baseline
provider via the Phase 10 admin endpoints before the run, so the comparison
happens under IDENTICAL adversarial conditions instead of random real-world
failures. Chaos is cleared again afterwards.

Results are written to results/scenario_{1,2}_results.json, each containing
aggregate metrics for both arms (the report generator reads both files).

Sanity gates: the script fails loudly if it cannot reach the backend, if
test_prompts.json is malformed, or if responses start hitting the API rate
limiter (poisoned data is worse than no data — bump ROUTE_RATE_LIMIT).
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# Windows consoles default to cp1252 and choke on the unicode glyphs used
# in the progress output — force UTF-8 before anything prints.
if hasattr(sys.stdout, "reconfigure") and (
    sys.stdout.encoding or ""
).lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Import the cost table directly from the backend so pricing stays in ONE
# place (scorer.py). Requires the backend venv / PYTHONPATH: run with
#   backend/.venv/Scripts/python.exe run_benchmark.py   (from benchmark/)
BENCHMARK_DIR = Path(__file__).resolve().parent
BACKEND_DIR = BENCHMARK_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.scorer import COST_PER_1K_TOKENS  # noqa: E402

RESULTS_DIR = BENCHMARK_DIR / "results"
PROMPTS_FILE = BENCHMARK_DIR / "test_prompts.json"

DEFAULT_BASELINE_PROVIDER = "gemini"
# See benchmark/README.md — gemini is the representative single model.
MAX_ROUTE_ATTEMPTS_FOR_NAIVE_ARM = 1

KNOWN_TASK_TYPES = {"code", "reasoning", "summarization", "qa", "general"}

SCENARIO_RESULTS_FILES = {
    "normal": "scenario_1_results.json",
    "chaos": "scenario_2_results.json",
}


class BenchmarkAbort(Exception):
    """Abort the whole benchmark arm — the data is not trustworthy."""


# --------------------------------------------------------------------------
# Startup sanity checks (fail loudly, per the task requirements)
# --------------------------------------------------------------------------


def load_and_validate_prompts() -> list[dict]:
    if not PROMPTS_FILE.exists():
        raise SystemExit(
            f"FATAL: {PROMPTS_FILE} not found. Run this script from the "
            "benchmark/ folder of a full clone."
        )
    try:
        data = json.loads(PROMPTS_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise SystemExit(f"FATAL: test_prompts.json is not valid JSON: {e}")

    prompts = data.get("prompts") if isinstance(data, dict) else None
    if not isinstance(prompts, list) or not prompts:
        raise SystemExit(
            "FATAL: test_prompts.json must contain a top-level object with a "
            "non-empty 'prompts' array: {\"prompts\": [{id, prompt, "
            "expected_task_type}, ...]}"
        )

    seen_ids: set[int] = set()
    for i, entry in enumerate(prompts):
        bad = lambda detail: SystemExit(  # noqa: E731
            f"FATAL: invalid prompt entry #{i} in test_prompts.json — {detail}"
        )
        if not isinstance(entry, dict):
            raise bad("entry is not an object")
        if not isinstance(entry.get("id"), int):
            raise bad("'id' must be an integer")
        if entry["id"] in seen_ids:
            raise bad(f"duplicate id {entry['id']}")
        seen_ids.add(entry["id"])
        if not isinstance(entry.get("prompt"), str) or not entry["prompt"].strip():
            raise bad("'prompt' must be a non-empty string")
        if entry.get("expected_task_type") not in KNOWN_TASK_TYPES:
            raise bad(
                f"'expected_task_type' must be one of {sorted(KNOWN_TASK_TYPES)}"
            )
    print(f"✓ test_prompts.json OK — {len(prompts)} prompts validated")
    return prompts


async def assert_backend_reachable(client: httpx.AsyncClient, base_url: str) -> None:
    try:
        resp = await client.get(f"{base_url}/api/v1/health", timeout=5.0)
    except httpx.HTTPError as e:
        raise SystemExit(
            f"FATAL: backend is not reachable at {base_url} ({e}).\n"
            "Start it first, e.g. from backend/: uvicorn app.main:app --reload\n"
            "(or: docker compose up). See benchmark/README.md."
        )
    if resp.status_code != 200:
        body = _safe_json(resp)
        raise SystemExit(
            f"FATAL: backend health check failed (HTTP {resp.status_code}).\n"
            f"Body: {json.dumps(body, indent=2)}\n"
            "Chaos benchmarking needs Redis reachable; fix the degraded "
            "component and re-run."
        )
    components = (resp.json() or {}).get("components", {})
    print(f"✓ backend reachable at {base_url} (components: {components})")


# --------------------------------------------------------------------------
# Chaos helpers (Phase 10 admin endpoints)
# --------------------------------------------------------------------------


async def chaos_inject(
    client: httpx.AsyncClient, base_url: str, provider: str, duration_seconds: int
) -> int:
    resp = await client.post(
        f"{base_url}/api/v1/admin/chaos/inject",
        json={"provider_name": provider, "duration_seconds": duration_seconds},
    )
    if resp.status_code == 404:
        raise SystemExit(
            f"FATAL: chaos endpoints return 404 — the backend is running with "
            "CHAOS_MODE_ENABLED=false. For the chaos scenario, set "
            "CHAOS_MODE_ENABLED=true in backend/.env and restart the backend."
        )
    if resp.status_code != 200:
        raise SystemExit(
            f"FATAL: chaos inject failed (HTTP {resp.status_code}): "
            f"{_safe_json(resp)}"
        )
    body = resp.json()
    print(
        f"⚡ chaos injected on '{body['provider_name']}' "
        f"(expires in {body['expires_in_seconds']}s)"
    )
    return body["expires_in_seconds"]


async def chaos_clear(client: httpx.AsyncClient, base_url: str, provider: str) -> None:
    resp = await client.post(
        f"{base_url}/api/v1/admin/chaos/clear", json={"provider_name": provider}
    )
    if resp.status_code == 200:
        print(f"⚡ chaos cleared on '{provider}' (clean state restored)")
    else:
        print(
            f"⚠ could not clear chaos on '{provider}' "
            f"(HTTP {resp.status_code}) — TTL will expire it eventually."
        )


# --------------------------------------------------------------------------
# One benchmark arm
# --------------------------------------------------------------------------


def build_arm_props(prompts: list[dict], arm: str, baseline_provider: str) -> list[dict]:
    """Per-prompt /route request bodies for the given arm."""
    if arm == "naive_baseline":
        return [
            {
                "prompt": p["prompt"],
                "task_type": p["expected_task_type"],
                "max_fallbacks": MAX_ROUTE_ATTEMPTS_FOR_NAIVE_ARM,
                "force_provider": baseline_provider,
            }
            for p in prompts
        ]
    return [
        {"prompt": p["prompt"], "task_type": p["expected_task_type"], "max_fallbacks": 3}
        for p in prompts
    ]


def estimate_cost(model_used: str | None, tokens_used: int) -> float:
    if model_used is None or tokens_used <= 0:
        return 0.0
    return (tokens_used / 1000.0) * COST_PER_1K_TOKENS.get(model_used, 0.0)


def record_to_row(prompt: dict, latency_ms: float, body: dict) -> dict:
    tokens = int(body.get("tokens_used") or 0)
    model_used = body.get("model_used")
    return {
        "id": prompt["id"],
        "expected_task_type": prompt["expected_task_type"],
        "success": bool(body.get("success")),
        "latency_ms": round(float(latency_ms), 2),
        "tokens_used": tokens,
        "model_used": model_used,
        "estimated_cost": round(estimate_cost(model_used, tokens), 8),
        "error": body.get("error") or (None if body.get("success") else "unknown_error"),
    }


def failure_row(prompt: dict, reason: str) -> dict:
    return {
        "id": prompt["id"],
        "expected_task_type": prompt["expected_task_type"],
        "success": False,
        "latency_ms": 0.0,
        "tokens_used": 0,
        "model_used": None,
        "estimated_cost": 0.0,
        "error": reason,
    }


def summarize(rows: list[dict], arm: str) -> dict:
    successes = [r for r in rows if r["success"]]
    total_cost = sum(r["estimated_cost"] for r in rows)
    avg_latency = (
        sum(r["latency_ms"] for r in rows) / len(rows) if rows else 0.0
    )
    breakdown: dict[str, dict] = {}
    for task_type in sorted({r["expected_task_type"] for r in rows}):
        subset = [r for r in rows if r["expected_task_type"] == task_type]
        task_successes = [r for r in subset if r["success"]]
        breakdown[task_type] = {
            "total_prompts": len(subset),
            "success_count": len(task_successes),
            "success_rate": round(len(task_successes) / len(subset), 4) if subset else 0.0,
            "avg_latency_ms": round(
                sum(r["latency_ms"] for r in subset) / len(subset), 2
            ) if subset else 0.0,
        }
    return {
        "arm": arm,
        "total_prompts": len(rows),
        "success_count": len(successes),
        "success_rate": round(len(successes) / len(rows), 4) if rows else 0.0,
        "avg_latency_ms": round(avg_latency, 2),
        "total_estimated_cost": round(total_cost, 8),
        "per_task_type_breakdown": breakdown,
        "per_prompt_results": rows,
    }


async def generate_demo_key(client: httpx.AsyncClient, base_url: str) -> str:
    """Create a lightweight demo API key for this benchmark run."""
    resp = await client.post(
        f"{base_url}/api/v1/keys/generate",
        json={"label": "RouteLLM benchmark"},
        timeout=30.0,
    )
    if resp.status_code != 201:
        raise BenchmarkAbort(
            f"Could not generate a benchmark API key: HTTP {resp.status_code} {resp.text[:120]}"
        )
    return resp.json()["api_key"]


async def run_arm(
    client: httpx.AsyncClient,
    base_url: str,
    prompts: list[dict],
    arm: str,
    baseline_provider: str,
    api_key: str,
) -> dict:
    props = build_arm_props(prompts, arm, baseline_provider)
    rows: list[dict] = []

    for prompt, prop in zip(prompts, props):
        started = time.perf_counter()
        try:
            resp = await client.post(
                f"{base_url}/api/v1/route",
                json=prop,
                headers={"X-API-Key": api_key},
                timeout=120.0,
            )
            wall_ms = (time.perf_counter() - started) * 1000.0

            if resp.status_code == 200:
                rows.append(record_to_row(prompt, wall_ms, resp.json()))
            elif resp.status_code == 429:
                # Rate limiting poisons the sample AND skews latency — abort
                # loudly rather than publish compromised numbers.
                raise BenchmarkAbort(
                    "HTTP 429 from POST /api/v1/route — the backend's rate "
                    "limiter kicked in. Bump ROUTE_RATE_LIMIT in the backend "
                    "env (e.g. ROUTE_RATE_LIMIT=100000/minute), restart, and "
                    "re-run the benchmark."
                )
            else:
                rows.append(
                    failure_row(prompt, f"http_{resp.status_code}:{resp.text[:120]}")
                )
                print(f"  ✗ prompt {prompt['id']}: HTTP {resp.status_code}")
        except (httpx.HTTPError, json.JSONDecodeError) as e:
            # Network hiccup mid-run: record as failure, keep going.
            rows.append(failure_row(prompt, f"request_exception:{type(e).__name__}"))
            print(f"  ✗ prompt {prompt['id']}: {type(e).__name__}: {e}")
        except BenchmarkAbort:
            raise

    label = "naive (single model, no fallback)" if arm == "naive_baseline" else "routellm (full system)"
    summary = summarize(rows, arm)
    print(
        f"  {label}: {summary['success_count']}/{summary['total_prompts']} "
        f"succeeded ({summary['success_rate']:.1%}), "
        f"avg {summary['avg_latency_ms']:.0f}ms, "
        f"cost ${summary['total_estimated_cost']:.6f}"
    )
    return summary


# --------------------------------------------------------------------------
# Scenario orchestration
# --------------------------------------------------------------------------


async def run_scenario(
    scenario: str,
    base_url: str,
    prompts: list[dict],
    baseline_provider: str,
) -> dict:
    chaos_scenario = scenario == "chaos"
    print(f"\n=== Scenario {1 if not chaos_scenario else 2}: "
          f"{'chaos injected' if chaos_scenario else 'normal conditions'} ===")

    async with httpx.AsyncClient() as client:
        await assert_backend_reachable(client, base_url)
        api_key = await generate_demo_key(client, base_url)

        if chaos_scenario:
            # Long TTL covers the whole run; the script still clears it at
            # the end so the backend is never left in a broken state.
            await chaos_inject(client, base_url, baseline_provider, duration_seconds=3600)

        results = {"runs": {}}
        for arm in ("naive_baseline", "routellm_full"):
            print(f"--- arm: {arm} ---")
            results["runs"].update(
                {arm: await run_arm(client, base_url, prompts, arm, baseline_provider, api_key)}
            )

        if chaos_scenario:
            await chaos_clear(client, base_url, baseline_provider)

    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark: naive single-model baseline vs RouteLLM full system."
    )
    parser.add_argument(
        "--scenario",
        choices=["normal", "chaos"],
        default="normal",
        help="normal = both arms under healthy providers; chaos = inject a "
        "deterministic outage on the baseline provider before both arms.",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the already-running backend (default: %(default)s).",
    )
    parser.add_argument(
        "--baseline-provider",
        default=DEFAULT_BASELINE_PROVIDER,
        help="Provider the naive arm hardcodes (default: %(default)s).",
    )
    args = parser.parse_args()

    prompts = load_and_validate_prompts()

    scenario = asyncio.run(
        run_scenario(args.scenario, args.base_url, prompts, args.baseline_provider)
    )

    RESULTS_DIR.mkdir(exist_ok=True)
    out_file = RESULTS_DIR / SCENARIO_RESULTS_FILES[args.scenario]
    payload = {
        "scenario": "normal_conditions" if args.scenario == "normal" else "chaos_simulated_outage",
        "chaos_injected": args.scenario == "chaos",
        "baseline_provider": args.baseline_provider,
        "run_name": out_file.stem,
        "prompts_file": str(PROMPTS_FILE),
        "total_prompts": len(prompts),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        **scenario,
    }
    out_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\n✓ written {out_file}")


def _safe_json(resp: httpx.Response):
    try:
        return resp.json()
    except json.JSONDecodeError:
        return {"raw": resp.text[:300]}


if __name__ == "__main__":
    main()
