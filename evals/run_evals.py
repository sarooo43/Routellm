"""Run the whole eval suite (E1-E4) with ONE command and write the report.

    python evals/run_evals.py                 # runs E1-E4, writes evals/results-mock/eval-results.md
    python evals/run_evals.py --no-docs       # print the report to stdout only
    python evals/run_evals.py --n 25          # smaller N for a quick smoke run

This suite is MOCKED-ONLY: deterministic scripted providers, no network, no
provider keys, no Redis. T4c contract: a mocked run must NEVER write to a
committed docs path - its report goes to ``evals/results-mock/`` (gitignored)
under a "MODE: MOCKED — scripted providers, not a measurement of any real
provider" header. A LIVE run (``python evals/run_resilience_eval.py --live
--yes-spend-quota``) is the only thing that writes ``docs/eval-results.md``
(and it writes a "MODE: LIVE" header with the date, commit and the models
actually called). The report is written with the real commit hash and date
and states plainly what these numbers do NOT show.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVALS_DIR.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(EVALS_DIR), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

#: Mocked runs write HERE (gitignored) - never to docs/ (T4c).
MOCK_DIR = EVALS_DIR / "results-mock"
MOCK_REPORT_PATH = MOCK_DIR / "eval-results.md"
#: docs/eval-results.md is written ONLY by live runs (run_resilience_eval.py
#: --live); kept as a constant so tests can assert mocked runs never touch it.
DOC_PATH = REPO_ROOT / "docs" / "eval-results.md"
MOCKED_HEADER_LINE = (
    "MODE: MOCKED — scripted providers, not a measurement of any real provider"
)

HEADER = f"""# Eval results

> **{MOCKED_HEADER_LINE}**
> All numbers in this file come from a MOCKED eval run (deterministic
> scripted providers, no network, no provider keys). They measure the
> gateway's routing/resilience/eval machinery - NOT real model quality.
> For real numbers, run `python evals/run_resilience_eval.py --live
> --yes-spend-quota` with your own provider keys configured; that writes
> docs/eval-results.md with a MODE: LIVE header instead.
"""

WHAT_THIS_DOES_NOT_SHOW = """## What this does not show

- **Nothing about real model quality.** The providers here are deterministic
  stubs with canned answers; the quality eval validates the checking
  machinery and the routing-accuracy link, not groq/deepseek/etc. themselves.
- **Nothing about real provider behavior under failure.** Failure scenarios
  are scripted at the transport boundary; real 429/403/404 semantics, Retry-
  After values and error shapes differ per provider.
- **Routing accuracy is only as good as its labels.** The 100 labeled
  prompts were authored for this eval, not independently verified; the
  classifier may behave differently on other prompt distributions.
- **Latency absolute values are not meaningful** in mocked mode (they
  include the scripted provider sleep); only the auto-vs-naive DELTAS
  isolate the router's own overhead.
- **Wilson intervals at n=50 are wide.** An observed 100% success rate does
  not mean the true rate is 100% - see the intervals.
- **No concurrency.** Requests run sequentially; burst behavior, retry
  storms and rate-limit interaction under load are out of scope.
- **Live mode was not exercised in this run.** `--live` (resilience) and
  real-key quality runs need the owner's keys and Chaos Mode.
"""


def git_commit_hash() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()[:12]
    except Exception:
        return "unknown"


def run_all(n_resilience: int, n_overhead: int) -> dict:
    import run_overhead_eval
    import run_quality_eval
    import run_resilience_eval
    import run_routing_eval

    silence = run_overhead_eval.silence_structlog
    silence()

    routing = run_routing_eval.run_routing_eval()
    resilience_scenarios = [
        run_resilience_eval.run_scenario_mocked(s, n_resilience, 3)
        for s in run_resilience_eval.SCENARIOS
    ]
    resilience = {
        "eval": "resilience",
        "mode": "mocked (deterministic, no network)",
        "n_per_scenario": n_resilience,
        "scenarios": resilience_scenarios,
    }
    quality = run_quality_eval.run_quality_eval()
    overhead = run_overhead_eval.run_overhead_eval(n_overhead)
    return {
        "routing": routing,
        "resilience": resilience,
        "quality": quality,
        "overhead": overhead,
    }


def render_report(blob: dict) -> str:
    import run_overhead_eval
    import run_quality_eval
    import run_resilience_eval
    import run_routing_eval

    parts = [
        HEADER.rstrip(),
        "",
        f"- Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Commit: `{git_commit_hash()}`",
        "- Mode: mocked (scripted providers; no provider keys, no network, "
        "no real model calls anywhere in this run)",
        f"- Models: scripted stubs named `{run_resilience_eval.PRIMARY}` and "
        f"`{run_resilience_eval.SECONDARY}` (deterministic canned answers, "
        "NOT the real providers)",
        f"- n: routing {blob['routing']['n']} labeled prompts; resilience "
        f"{blob['resilience']['n_per_scenario']} requests x 5 scenarios x 2 arms; "
        f"quality {blob['quality']['n_tasks']} tasks x 3 arms "
        "(not measured: scripted stubs); overhead "
        f"{blob['overhead']['auto']['n']} requests x 2 arms",
        "- Intervals: Wilson 95% on every success rate below",
        "",
        "---",
        "",
        run_routing_eval.render_markdown(blob["routing"]),
        "",
        "---",
        "",
        run_resilience_eval.render_markdown(
            blob["resilience"]["scenarios"],
            blob["resilience"]["n_per_scenario"],
            blob["resilience"]["mode"],
        ),
        "",
        "---",
        "",
        run_quality_eval.render_markdown(blob["quality"]),
        "",
        "---",
        "",
        run_overhead_eval.render_markdown(blob["overhead"]),
        "",
        "---",
        "",
        WHAT_THIS_DOES_NOT_SHOW.rstrip(),
        "",
        "## Reproduce / go real",
        "",
        "```bash",
        "python evals/run_evals.py                          # mocked suite -> evals/results-mock/eval-results.md (gitignored)",
        "python evals/run_routing_eval.py                   # E1 only (no provider calls at all)",
        "python evals/run_resilience_eval.py --live --yes-spend-quota --base-url http://127.0.0.1:8000   # LIVE -> docs/eval-results.md",
        "python evals/run_quality_eval.py --json",
        "python evals/run_overhead_eval.py --json",
        "```",
        "",
        "Mocked runs never write to `docs/` (T4c): this report lives in the "
        "gitignored `evals/results-mock/`. A LIVE resilience run — which "
        "refuses to start without `--yes-spend-quota`, at least 2 saved "
        "provider keys and `CHAOS_MODE_ENABLED` — writes "
        "`docs/eval-results.md` with a `MODE: LIVE` header, the date, the "
        "commit and the models actually called; THAT is the file to commit "
        "after a real run.",
        "",
    ]
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the full mocked eval suite and write evals/results-mock/eval-results.md."
    )
    parser.add_argument("--n", type=int, default=50, help="requests per resilience scenario")
    parser.add_argument("--n-overhead", type=int, default=200, help="requests per overhead arm")
    parser.add_argument("--no-docs", action="store_true", help="print the report, do not write it")
    parser.add_argument("--json", action="store_true", help="also print the raw result blob")
    args = parser.parse_args()

    blob = run_all(args.n, args.n_overhead)
    report = render_report(blob)
    # Undo the log silencing so an embedded caller (tests) keeps a working
    # logger; harmless in CLI use where the process exits anyway.
    from harness import restore_structlog

    restore_structlog()

    if args.json:
        print(json.dumps(blob, indent=2))
    if args.no_docs:
        print(report)
    else:
        # T4c: a mocked run NEVER writes to a committed docs path. It writes
        # to the gitignored evals/results-mock/ under a MOCKED header; only a
        # live run (run_resilience_eval.py --live --yes-spend-quota) writes
        # docs/eval-results.md, with a MODE: LIVE header.
        MOCK_DIR.mkdir(parents=True, exist_ok=True)
        MOCK_REPORT_PATH.write_text(report, encoding="utf-8")
        try:
            shown = MOCK_REPORT_PATH.relative_to(REPO_ROOT)
        except ValueError:
            shown = MOCK_REPORT_PATH
        print(f"wrote {shown} (gitignored: mocked results are never committed)")
        print(f"routing accuracy: {blob['routing']['accuracy'] * 100:.1f}% | "
              f"resilience router success: "
              f"{blob['resilience']['scenarios'][0]['router']['success_rate'] * 100:.0f}% "
              f"(first scenario) | quality: not measured (mocked stubs) | "
              f"overhead delta p50: {blob['overhead']['overhead_delta']['p50_ms']} ms")


if __name__ == "__main__":
    main()
