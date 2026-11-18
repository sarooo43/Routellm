"""E3 - Quality eval: checkable tasks only, no LLM judge.

Every task has a mechanically checkable answer:
    math - exact match on the final numeric answer
    code - the produced Python is exec'd and must pass bundled unit tests
    json - the produced JSON must validate against a schema (mini validator:
           type / properties / required / items / additionalProperties)

Three arms over the same tasks:
    auto            - the real router classifies and ranks, no forcing
    pinned_groq     - force_provider="groq"  (one hardcoded model)
    pinned_deepseek - force_provider="deepseek"

MOCKED MODE (the only mode in this repo run): the two providers are
deterministic stubs with canned per-task answers - groq answers every task
correctly, deepseek only the JSON kind. This validates the routing +
checking MACHINERY, not real model quality: with real keys the same script
measures actual provider answers. No numbers here say anything about real
models.

Run:
    python evals/run_quality_eval.py          # human tables
    python evals/run_quality_eval.py --json   # machine blob
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
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
    restore_structlog,
    silence_structlog,
    wilson_interval,
)
from app.providers.registry import PROVIDER_REGISTRY  # noqa: E402

TASKS_PATH = EVALS_DIR / "quality_tasks.jsonl"
PINNED = ["groq", "deepseek"]
KINDS = ("math", "code", "json")


def load_tasks(path: Path = TASKS_PATH) -> list[dict]:
    """Load + validate the task set; fail loudly on a bad file."""
    if not path.exists():
        raise SystemExit(f"FATAL: task set {path} not found (run from the repo).")
    rows: list[dict] = []
    seen_ids: set[int] = set()
    seen_prompts: set[str] = set()
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"FATAL: line {line_no} is not valid JSON: {exc}")
            kind = row.get("kind")
            if kind not in KINDS:
                raise SystemExit(f"FATAL: line {line_no}: unknown kind {kind!r}")
            if not row.get("prompt") or not row.get("answers"):
                raise SystemExit(f"FATAL: line {line_no}: missing prompt/answers")
            for provider in PINNED:
                if provider not in row["answers"]:
                    raise SystemExit(f"line {line_no}: no canned answer for {provider}")
            if kind == "math" and "expected" not in row:
                raise SystemExit(f"FATAL: line {line_no}: math task without expected")
            if kind == "code" and not row.get("tests"):
                raise SystemExit(f"FATAL: line {line_no}: code task without tests")
            if kind == "json" and "schema" not in row:
                raise SystemExit(f"FATAL: line {line_no}: json task without schema")
            if row["id"] in seen_ids or row["prompt"] in seen_prompts:
                raise SystemExit(f"FATAL: line {line_no}: duplicate id/prompt")
            seen_ids.add(row["id"])
            seen_prompts.add(row["prompt"])
            rows.append(row)
    if len(rows) < 3 * len(KINDS):
        raise SystemExit(f"FATAL: need at least {3 * len(KINDS)} tasks, got {len(rows)}")
    return rows


# ---------------------------------------------------------------------------
# Checkers (no LLM in the loop, ever)
# ---------------------------------------------------------------------------


def check_math(text: str, expected) -> bool:
    """Exact match on the LAST number in the reply (the 'Answer: N' value)."""
    numbers = re.findall(r"-?\d+(?:\.\d+)?", text or "")
    if not numbers:
        return False
    try:
        return abs(float(numbers[-1]) - float(expected)) < 1e-9
    except ValueError:
        return False


def extract_code(text: str) -> str:
    """Pull the python block out of a reply (fenced, else the whole text)."""
    fenced = re.findall(r"```(?:python)?\s*\n(.*?)```", text or "", flags=re.DOTALL)
    return fenced[0] if fenced else (text or "")


def check_code(text: str, tests: list[str]) -> bool:
    """Exec the produced code, then each bundled test. Any failure = wrong.

    NOTE: this exec's dataset-authored code (the repo's own canned answers),
    not arbitrary network input - the task set is version-controlled.
    """
    code = extract_code(text)
    if not code.strip():
        return False
    namespace: dict = {}
    try:
        exec(code, namespace)  # noqa: S102 - repo-controlled eval dataset
        for test in tests:
            exec(test, namespace)  # noqa: S102 - repo-controlled eval dataset
    except Exception:
        return False
    return True


_JSON_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def validate_against_schema(value, schema: dict, path: str = "$") -> list[str]:
    """Mini JSON-schema validator: the subset this eval needs (type,
    properties, required, items, additionalProperties). Exists because
    ``jsonschema`` is not a backend dependency; if it ever becomes one,
    swap this function out wholesale."""
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type:
        py_type = _JSON_TYPES.get(expected_type)
        ok = isinstance(value, py_type) if py_type else True
        # bool is a subclass of int: exclude it from "number"
        if ok and expected_type == "number" and isinstance(value, bool):
            ok = False
        if not ok:
            return [f"{path}: expected {expected_type}, got {type(value).__name__}"]
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errors.append(f"{path}: missing required property {req!r}")
        for name, sub in schema.get("properties", {}).items():
            if name in value:
                errors.extend(validate_against_schema(value[name], sub, f"{path}.{name}"))
        if schema.get("additionalProperties") is False:
            known = set(schema.get("properties", {}))
            extra = set(value) - known
            if extra:
                errors.append(f"{path}: unexpected properties {sorted(extra)}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors.extend(validate_against_schema(item, schema["items"], f"{path}[{i}]"))
    return errors


def extract_json(text: str):
    """Parse the JSON value out of a reply (tolerates code fences / prose)."""
    cleaned = (text or "").strip()
    if "```" in cleaned:
        blocks = re.findall(r"```(?:json)?\s*\n(.*?)```", cleaned, flags=re.DOTALL)
        if blocks:
            cleaned = blocks[0].strip()
    try:
        return json.loads(cleaned), None
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1:
            start, end = cleaned.find("["), cleaned.rfind("]")
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1]), None
            except json.JSONDecodeError:
                pass
    return None, "not parseable as JSON"


def check_json(text: str, schema: dict) -> bool:
    value, err = extract_json(text)
    if err is not None:
        return False
    return not validate_against_schema(value, schema)


def check_task(row: dict, text: str) -> bool:
    kind = row["kind"]
    if kind == "math":
        return check_math(text, row["expected"])
    if kind == "code":
        return check_code(text, row["tests"])
    return check_json(text, row["schema"])


# ---------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------


def install_scripted_providers(tasks: list[dict]) -> dict:
    """Script both providers from the canned answers, keyed by prompt.

    Everything else in the registry is removed for the run: the local env
    may carry operator keys and the router would make a REAL call.
    """
    by_prompt = {row["prompt"]: row for row in tasks}

    def behavior_for(provider: str):
        def behavior(prompt: str, call_index: int):
            row = by_prompt.get(prompt)
            text = row["answers"][provider] if row else "scripted answer"
            return make_scripted_response(provider, f"{provider}-scripted-model", text=text)

        return behavior

    stubs = {
        name: ScriptedProvider(name, f"{name}-scripted-model", behavior_for(name))
        for name in PINNED
    }
    saved = dict(PROVIDER_REGISTRY)
    PROVIDER_REGISTRY.clear()
    PROVIDER_REGISTRY.update(stubs)
    return saved


def restore_registry(saved: dict) -> None:
    PROVIDER_REGISTRY.clear()
    PROVIDER_REGISTRY.update(saved)


async def run_arm(tasks: list[dict], arm: str) -> dict:
    """Run every task through one arm; check each reply mechanically."""
    from app.core.executor import execute_with_fallback

    maker, breaker, quota, chaos = await build_eval_stack()
    per_kind = {k: {"n": 0, "correct": 0} for k in KINDS}
    providers_used: dict[str, int] = {}
    async with maker() as session:
        for row in tasks:
            kwargs = {}
            if arm != "auto":
                kwargs["force_provider"] = arm.replace("pinned_", "")
            result = await execute_with_fallback(
                row["prompt"],
                session,
                max_fallbacks=3,
                circuit_breaker=breaker,
                quota_tracker=quota,
                chaos_controller=chaos,
                **kwargs,
            )
            text = result.result_text if result.success else ""
            correct = check_task(row, text)
            per_kind[row["kind"]]["n"] += 1
            if correct:
                per_kind[row["kind"]]["correct"] += 1
            provider = (result.model_used or "").split("/")[0] or "unknown"
            providers_used[provider] = providers_used.get(provider, 0) + 1
    n = len(tasks)
    correct = sum(v["correct"] for v in per_kind.values())
    lo, hi = wilson_interval(correct, n)
    return {
        "arm": arm,
        "n": n,
        "correct": correct,
        "accuracy": round(correct / n, 4) if n else 0.0,
        "wilson_95": [round(lo, 4), round(hi, 4)],
        "per_kind": {
            k: {
                **v,
                "accuracy": round(v["correct"] / v["n"], 4) if v["n"] else None,
            }
            for k, v in per_kind.items()
        },
        "providers_used": providers_used,
    }


def run_quality_eval(tasks_path: Path = TASKS_PATH) -> dict:
    tasks = load_tasks(tasks_path)
    saved = install_scripted_providers(tasks)
    try:
        arms = [
            asyncio.run(run_arm(tasks, "auto")),
            asyncio.run(run_arm(tasks, "pinned_groq")),
            asyncio.run(run_arm(tasks, "pinned_deepseek")),
        ]
    finally:
        restore_registry(saved)
    return {
        "eval": "quality",
        "tasks_file": str(tasks_path.relative_to(EVALS_DIR.parent)),
        "n_tasks": len(tasks),
        "mode": "mocked (scripted providers, deterministic; no real models)",
        "arms": arms,
    }


def render_markdown(result: dict) -> str:
    # T4d: quality percentages are a LIVE-mode-only output. A mocked run's
    # "accuracy" describes the canned scripts, not a model, so it prints
    # "not measured: scripted stubs" instead of any percentage.
    mocked = str(result.get("mode", "")).startswith("mocked")
    lines = [
        "## Quality (E3)",
        "",
        f"Mode: **{result['mode']}** - {result['n_tasks']} checkable tasks "
        "(8 exact-match math, 8 code with unit tests, 8 JSON schema), three "
        "arms, every reply checked mechanically. NO LLM judge anywhere.",
    ]
    if mocked:
        lines += [
            "",
            "**Quality: not measured: scripted stubs.** No accuracy or "
            "success percentages are reported for a mocked run - the "
            "providers answer from canned scripts (groq scripted correct on "
            "every task, deepseek only on JSON), so a percentage here would "
            "describe the script, not a model. Percentages appear only in a "
            "LIVE run with real keys, where each column also carries the "
            "per-task-type n.",
            "",
            "| Arm | Accuracy | math | code | json | provider used |",
            "|---|---|---|---|---|---|",
        ]
        for arm in result["arms"]:
            used = ", ".join(
                f"{p} x{n}" for p, n in sorted(arm["providers_used"].items(), reverse=True)
            )
            lines.append(
                f"| {arm['arm']} | not measured (scripted stubs) "
                f"| not measured | not measured | not measured | {used} |"
            )
        return "\n".join(lines)
    lines += [
        "",
        "**What these numbers mean:** this is a LIVE run against real "
        "providers - the percentages below are measured, and each column "
        "carries the n per task type.",
        "",
        "| Arm | Accuracy | Wilson 95% | math (n, acc) | code (n, acc) | json (n, acc) | provider used |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in result["arms"]:
        pk = arm["per_kind"]
        fmt = lambda kind: (  # noqa: E731
            f"n={pk[kind]['n']}, {pk[kind]['accuracy'] * 100:.1f}%"
            if pk[kind]["accuracy"] is not None
            else "n=0, n/a"
        )
        lo, hi = arm["wilson_95"]
        used = ", ".join(f"{p} x{n}" for p, n in sorted(arm["providers_used"].items(), reverse=True))
        lines.append(
            f"| {arm['arm']} | {arm['accuracy'] * 100:.1f}% ({arm['correct']}/{arm['n']}) "
            f"| [{lo * 100:.1f}%, {hi * 100:.1f}%] | {fmt('math')} "
            f"| {fmt('code')} | {fmt('json')} | {used} |"
        )
    return "\n".join(lines)


def main() -> None:
    silence_structlog()
    try:
        parser = argparse.ArgumentParser(
            description="E3: quality eval - Auto vs pinned models on checkable tasks."
        )
        parser.add_argument("--json", action="store_true", help="print the raw result blob")
        parser.add_argument("--tasks", default=str(TASKS_PATH), help="path to the task JSONL")
        args = parser.parse_args()

        result = run_quality_eval(Path(args.tasks))
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
