"""Part A of the ML routing phase — collect judge-scored training data.

Standalone OFFLINE script (never imported by the FastAPI app). For every
prompt it calls EVERY provider in PROVIDER_REGISTRY directly (bypassing
routing/fallback — we want raw responses to all providers of the same
prompt to compare), then asks the judge LLM to rate each response 0..1.

Runs against provider APIs directly using backend/.env credentials — the
backend server does NOT need to be running:

    backend/.venv/Scripts/python.exe scripts/collect_training_data.py
    backend/.venv/Scripts/python.exe scripts/collect_training_data.py \
        --prompts /path/to/extra_prompts.json

Output: backend/scripts/training_data/raw_responses.jsonl — one JSON line
per (prompt, provider) pair:
    {"prompt", "provider", "success", "quality_score", "latency_ms",
     "tokens_used", "task_type"}

RESUMABLE: combos already present in the output file are skipped, so a
crashed/rate-limited run can simply be re-invoked and it continues where
it left of, never re-spending API calls for recorded pairs.
"""

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure") and (
    sys.stdout.encoding or ""
).lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Run from backend/ (or with backend on PYTHONPATH) so app.* imports work.
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.classifier import classify_task  # noqa: E402
from app.core.judge import judge_response_quality  # noqa: E402
from app.providers.registry import PROVIDER_REGISTRY  # noqa: E402
from app.utils.logger import get_logger  # noqa: E402

logger = get_logger("collect_training_data")

DEFAULT_PROMPTS_FILE = BACKEND_DIR.parent / "benchmark" / "test_prompts.json"
DEFAULT_OUT_FILE = Path(__file__).resolve().parent / "training_data" / "raw_responses.jsonl"


def prompt_key(prompt: str) -> str:
    """Stable key for the resumability index."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def load_prompts(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    prompts = data.get("prompts") if isinstance(data, dict) else data
    if not isinstance(prompts, list) or not prompts:
        raise SystemExit(f"FATAL: {path} must contain a non-empty prompts array")
    for entry in prompts:
        if not isinstance(entry, dict) or not entry.get("prompt", "").strip():
            raise SystemExit(f"FATAL: malformed prompt entry in {path}: {entry!r}")
    return prompts


def existing_combos(out_file: Path) -> set[str]:
    """(prompt_key, provider) pairs already recorded — the resume index."""
    done: set[str] = set()
    if out_file.exists():
        for line in out_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                done.add(f"{prompt_key(row['prompt'])}|{row['provider']}")
            except (json.JSONDecodeError, KeyError):
                # Corrupted trailing line (crashed mid-write): ignore it.
                logger.warning("skipping_corrupt_line_in_out_file")
    return done


def deepseek_kwargs(task_type: str) -> dict:
    # Mirror the executor's variant routing for deepseek.
    return {
        "model_variant": "deepseek-coder" if task_type == "code" else "deepseek-chat",
    }


async def collect(args) -> None:
    prompts = load_prompts(args.prompts)
    done = existing_combos(args.out)
    pending = [
        p for p in prompts
        if any(f"{prompt_key(p['prompt'])}|{name}" not in done
               for name in PROVIDER_REGISTRY)
    ]
    print(
        f"collecting: {len(pending)} new prompt(s) x {len(PROVIDER_REGISTRY)} "
        f"provider(s) — {len(prompts) - len(pending)} already recorded (resumed)"
    )
    if not pending:
        print("nothing to do — dataset already complete")
        return

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("a", encoding="utf-8") as sink:
        for index, entry in enumerate(pending, 1):
            prompt = entry["prompt"]
            task_type = classify_task(prompt)  # feature/label reference only
            print(
                f"\n[{index}/{len(pending)}] prompt #{entry.get('id', '?')} "
                f"(task_type={task_type}, {len(prompt)} chars)"
            )

            for provider_name, provider in PROVIDER_REGISTRY.items():
                record_key = f"{prompt_key(prompt)}|{provider_name}"
                if record_key in done:
                    continue

                started = time.perf_counter()
                kwargs = (
                    deepseek_kwargs(task_type) if provider_name == "deepseek" else {}
                )
                response = await provider.generate(prompt, **kwargs)
                wall_ms = (time.perf_counter() - started) * 1000.0

                if response.success:
                    quality = await judge_response_quality(prompt, response.text)
                else:
                    # No text to judge → no quality signal. Recording the
                    # failure keeps the dataset honest about coverage.
                    quality = 0.0

                row = {
                    "prompt": prompt,
                    "provider": provider_name,
                    "success": bool(response.success),
                    "quality_score": quality,
                    "latency_ms": round(wall_ms, 2),
                    "tokens_used": int(response.tokens_used or 0),
                    "task_type": task_type,
                    "collected_at": datetime.now(timezone.utc).isoformat(),
                }
                sink.write(json.dumps(row, ensure_ascii=False) + "\n")
                sink.flush()  # flush per row: a crash never loses progress
                done.add(record_key)
                status = (
                    f"ok (quality={quality:.2f}, {row['tokens_used']} tok)"
                    if response.success
                    else f"FAILED provider call ({response.error})"
                )
                print(f"    {provider_name:>10}: {status}")

            if index < len(pending) and args.delay_seconds > 0:
                time.sleep(args.delay_seconds)  # gentle on provider rate limits

    print(f"\ndone — dataset at {args.out}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect judge-scored (prompt, provider) training data."
    )
    parser.add_argument(
        "--prompts",
        type=Path,
        default=DEFAULT_PROMPTS_FILE,
        help="Prompt source (default: benchmark/test_prompts.json). Any extra "
        "JSON file with the same {\"prompts\": [{\"id\", \"prompt\", ...}]} "
        "shape can be passed to grow the dataset.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT_FILE,
        help="Output JSONL (default: scripts/training_data/raw_responses.jsonl)",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=1.0,
        help="Pause between prompts to stay under provider rate limits.",
    )
    args = parser.parse_args()
    asyncio.run(collect(args))


if __name__ == "__main__":
    main()
