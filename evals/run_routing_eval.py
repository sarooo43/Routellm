"""E1 - Routing accuracy eval (no provider calls, no network, no keys).

Measures how well the gateway's LOCAL classifier (``app.core.classifier``)
agrees with a human-labeled prompt set: overall accuracy, a per-class
confusion matrix, and a confidence calibration table (reliability bins).

This eval never touches a provider, the network, or the database: the
classifier is pure string/regex logic, so it runs anywhere (laptop, CI).

Run:
    python evals/run_routing_eval.py            # human-readable tables
    python evals/run_routing_eval.py --json     # machine-readable blob

Labels are ground truth ("what a person would call this task"), NOT what the
classifier happens to produce - disagreement is exactly what we measure.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = EVALS_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core import classifier as local_classifier  # noqa: E402

DATASET_PATH = EVALS_DIR / "routing_labeled.jsonl"

KNOWN_LABELS = {"code", "reasoning", "summarization", "qa", "general"}
KNOWN_DIFFICULTY = {"easy", "medium", "hard"}

#: Reliability-bin edges for the confidence calibration table.
BIN_EDGES = (0.0, 0.4, 0.6, 0.8, 1.0001)


#: Provenance header (T4e). The first line of the dataset JSONL carries the
#: label provenance; the eval reports it verbatim so an unreviewed dataset
#: can never be mistaken for a human-audited one.
HEADER_KEY = "_dataset_header"


def load_labeled_prompts(path: Path = DATASET_PATH) -> list[dict]:
    """Load and validate the labeled dataset; fail loudly on a bad file.

    An optional FIRST line ``{"_dataset_header": {...}}`` carries the label
    provenance (T4e) and is returned separately from the prompt rows.
    """
    if not path.exists():
        raise SystemExit(f"FATAL: dataset {path} not found (run from the repo).")
    rows: list[dict] = []
    header: dict | None = None
    seen_prompts: set[str] = set()
    seen_ids: set[int] = set()
    with path.open(encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"FATAL: line {line_no} is not valid JSON: {exc}")
            if HEADER_KEY in row:
                if line_no != 1:
                    raise SystemExit(
                        f"FATAL: line {line_no}: {HEADER_KEY} may only appear on line 1"
                    )
                header = row[HEADER_KEY]
                continue
            missing = [k for k in ("id", "prompt", "expected_task_type", "difficulty") if k not in row]
            if missing:
                raise SystemExit(f"FATAL: line {line_no} missing fields: {missing}")
            if row["expected_task_type"] not in KNOWN_LABELS:
                raise SystemExit(f"FATAL: line {line_no}: unknown label {row['expected_task_type']!r}")
            if row["difficulty"] not in KNOWN_DIFFICULTY:
                raise SystemExit(f"FATAL: line {line_no}: unknown difficulty {row['difficulty']!r}")
            if row["prompt"] in seen_prompts:
                raise SystemExit(f"FATAL: line {line_no}: duplicate prompt text")
            if row["id"] in seen_ids:
                raise SystemExit(f"FATAL: line {line_no}: duplicate id {row['id']}")
            seen_prompts.add(row["prompt"])
            seen_ids.add(row["id"])
            rows.append(row)
    if len(rows) < 100:
        raise SystemExit(f"FATAL: dataset has {len(rows)} prompts; the eval contract requires >= 100.")
    return rows


def dataset_provenance(rows: list[dict], path: Path = DATASET_PATH) -> dict:
    """Label provenance + keyword-leakage indicator (T4e).

    Leakage: the fraction of prompts containing at least one keyword from
    the rule-based classifier's OWN keyword lists. When the eval's prompts
    share vocabulary with the classifier, accuracy is likely INFLATED - the
    number is reported so nobody mistakes it for an independent sample.
    """
    header: dict | None = None
    if path.exists():
        first = path.open(encoding="utf-8").readline().strip()
        try:
            parsed = json.loads(first)
            if HEADER_KEY in parsed:
                header = parsed[HEADER_KEY]
        except json.JSONDecodeError:
            pass

    keyword_lists = [
        getattr(local_classifier, name)
        for name in ("CODE_KEYWORDS", "REASONING_KEYWORDS", "SUMMARIZATION_KEYWORDS")
        if hasattr(local_classifier, name)
    ]
    leaked = 0
    for row in rows:
        text = row["prompt"].lower()
        if any(kw.lower() in text for kws in keyword_lists for kw in kws):
            leaked += 1
    fraction = round(leaked / len(rows), 4) if rows else 0.0
    return {
        "authored_by": (header or {}).get("authored_by", "unknown"),
        "reviewed_by_owner": bool((header or {}).get("reviewed_by_owner", False)),
        "review_procedure": "evals/REVIEW.md",
        "keyword_leakage_fraction": fraction,
        "keyword_leakage_note": (
            f"{leaked}/{len(rows)} prompts contain a keyword from the "
            "classifier's own keyword lists - accuracy on this dataset is "
            "likely INFLATED relative to unseen prompts; treat it as an "
            "in-distribution self-check, not an independent measurement."
        ),
    }


def predict_with_confidence(prompt: str) -> tuple[str, float]:
    """The real classifier's decision plus a transparent confidence estimate.

    ``classify_task()`` returns only the label, so confidence is derived from
    the same public keyword/pattern constants the rule-based tiers match on.
    The estimate is deliberately simple and monotone in matched-signal
    strength: more (and multi-kind) evidence for the winning tier means a
    higher number. It is a ranking signal for calibration purposes, not a
    probability claim.
    """
    text = prompt.strip().lower()

    code_hits = sum(1 for kw in local_classifier.CODE_KEYWORDS if kw in text)
    code_hits += sum(1 for pat in local_classifier.CODE_PATTERNS if pat.search(text))
    reasoning_hits = sum(1 for kw in local_classifier.REASONING_KEYWORDS if kw in text)
    summarization_hits = sum(1 for kw in local_classifier.SUMMARIZATION_KEYWORDS if kw in text)
    if len(text) > local_classifier.LONG_PROMPT_THRESHOLD:
        summarization_hits += 1

    predicted = local_classifier.classify_task(prompt)
    tier_hits = {
        "code": code_hits,
        "reasoning": reasoning_hits,
        "summarization": summarization_hits,
        # qa/general tiers are structural (short + question mark) or default.
        "qa": 1 if (predicted == "qa" and text.endswith("?")) else 0,
        "general": 0,
    }
    winner_hits = tier_hits.get(predicted, 0)
    rival_hits = max(v for k, v in tier_hits.items() if k != predicted) if predicted in tier_hits else 0
    # 0.2 base (a default-tier decision is weak evidence), +0.2 per extra hit
    # beyond the first, capped at 1.0. Rival evidence shaves a little off.
    confidence = min(1.0, 0.2 + 0.2 * max(0, winner_hits - 1)) - 0.1 * min(rival_hits, 2)
    return predicted, max(0.05, round(confidence, 2))


def bin_index(confidence: float) -> int:
    for i in range(len(BIN_EDGES) - 1):
        if BIN_EDGES[i] <= confidence < BIN_EDGES[i + 1]:
            return i
    return len(BIN_EDGES) - 2


def run_routing_eval(dataset_path: Path = DATASET_PATH) -> dict:
    """Classify every labeled prompt locally; report accuracy/matrix/calibration."""
    rows = load_labeled_prompts(dataset_path)

    confusion: dict[str, dict[str, int]] = {label: defaultdict(int) for label in KNOWN_LABELS}
    per_difficulty_total: Counter = Counter()
    per_difficulty_correct: Counter = Counter()
    bins: list[dict] = [
        {
            "range": [BIN_EDGES[i], min(BIN_EDGES[i + 1], 1.0)],
            "n": 0,
            "mean_confidence": 0.0,
            "confidence_sum": 0.0,
            "correct": 0,
        }
        for i in range(len(BIN_EDGES) - 1)
    ]
    misclassified: list[dict] = []

    for row in rows:
        truth = row["expected_task_type"]
        predicted, confidence = predict_with_confidence(row["prompt"])
        correct = predicted == truth
        confusion[truth][predicted] += 1
        per_difficulty_total[row["difficulty"]] += 1
        if correct:
            per_difficulty_correct[row["difficulty"]] += 1
        else:
            misclassified.append(
                {"id": row["id"], "expected": truth, "predicted": predicted, "difficulty": row["difficulty"]}
            )
        b = bins[bin_index(confidence)]
        b["n"] += 1
        b["confidence_sum"] += confidence
        if correct:
            b["correct"] += 1

    n = len(rows)
    correct_total = sum(confusion[t].get(t, 0) for t in confusion)
    accuracy = correct_total / n
    provenance = dataset_provenance(rows, dataset_path)

    per_class = {}
    for label in sorted(KNOWN_LABELS):
        tp = confusion[label][label]
        predicted_as = sum(confusion[label].values())
        predicted_count = sum(confusion[t][label] for t in confusion)
        per_class[label] = {
            "support": predicted_as,
            "recall": round(tp / predicted_as, 4) if predicted_as else 0.0,
            "precision": round(tp / predicted_count, 4) if predicted_count else 0.0,
        }

    calibration = []
    for b in bins:
        calibration.append(
            {
                "range": b["range"],
                "n": b["n"],
                "mean_confidence": round(b["confidence_sum"] / b["n"], 3) if b["n"] else None,
                "accuracy": round(b["correct"] / b["n"], 4) if b["n"] else None,
            }
        )

    return {
        "eval": "routing_accuracy",
        "dataset": str(dataset_path.relative_to(EVALS_DIR.parent)),
        "n": n,
        "accuracy": round(accuracy, 4),
        "provenance": provenance,
        "per_difficulty": {
            d: {
                "n": per_difficulty_total[d],
                "accuracy": round(per_difficulty_correct[d] / per_difficulty_total[d], 4)
                if per_difficulty_total[d]
                else None,
            }
            for d in ("easy", "medium", "hard")
        },
        "confusion_matrix": {t: dict(sorted(preds.items())) for t, preds in sorted(confusion.items())},
        "per_class": per_class,
        "calibration_bins": calibration,
        "misclassified": misclassified,
        "provider_calls": 0,
    }


def render_markdown(result: dict) -> str:
    """Human-readable report block for the console and docs/eval-results.md."""
    lines = [
        "## Routing accuracy (E1)",
        "",
        f"Dataset: `{result['dataset']}` - {result['n']} labeled prompts, "
        f"mixed difficulty, no duplicates. **No provider calls made** "
        f"(`provider_calls: {result['provider_calls']}`): this measures the "
        f"local rule-based classifier only.",
        "",
        "**Label provenance (T4e):** authored_by="
        f"{result['provenance']['authored_by']}, reviewed_by_owner="
        f"{str(result['provenance']['reviewed_by_owner']).lower()} "
        f"(review procedure: {result['provenance']['review_procedure']}).",
        "",
        f"**Keyword leakage indicator:** "
        f"{result['provenance']['keyword_leakage_fraction'] * 100:.0f}% of "
        "the prompts contain a keyword from the classifier's own keyword "
        "lists. **Accuracy on this dataset may be inflated** - it is an "
        "in-distribution self-check against the classifier's own "
        "vocabulary, not an independent measurement.",
        "",
        f"**Accuracy: {result['accuracy'] * 100:.1f}%**",
        "",
        "Per difficulty:",
        "",
        "| Difficulty | n | Accuracy |",
        "|---|---|---|",
    ]
    for d, v in result["per_difficulty"].items():
        acc = f"{v['accuracy'] * 100:.1f}%" if v["accuracy"] is not None else "n/a"
        lines.append(f"| {d} | {v['n']} | {acc} |")
    lines += ["", "Per-class precision / recall:", "", "| Class | Support | Precision | Recall |", "|---|---|---|---|"]
    for label, v in result["per_class"].items():
        lines.append(f"| {label} | {v['support']} | {v['precision'] * 100:.1f}% | {v['recall'] * 100:.1f}% |")
    lines += [
        "",
        "Confusion matrix (rows = human label, columns = classifier decision):",
        "",
    ]
    labels = sorted(result["confusion_matrix"])
    header = "| truth \\\\ predicted | " + " | ".join(labels) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(labels) + 1))
    for t in labels:
        cells = [str(result["confusion_matrix"][t].get(p, 0)) for p in labels]
        lines.append(f"| {t} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "Confidence calibration (reliability bins; ideal: accuracy tracks mean confidence):",
        "",
        "| Confidence bin | n | Mean confidence | Observed accuracy |",
        "|---|---|---|---|",
    ]
    for b in result["calibration_bins"]:
        lo, hi = b["range"]
        mean_c = f"{b['mean_confidence']:.2f}" if b["mean_confidence"] is not None else "n/a"
        acc = f"{b['accuracy'] * 100:.1f}%" if b["accuracy"] is not None else "n/a"
        lines.append(f"| [{lo:.1f}, {hi:.1f}) | {b['n']} | {mean_c} | {acc} |")
    lines += [
        "",
        "Note (T4f): the confidence column is a DERIVED estimate computed in "
        "`predict_with_confidence` (0.2 base + 0.2 per extra keyword hit, "
        "capped), not a probability from the classifier itself - a prompt "
        "with a single keyword match scores 0.2 by construction, so most "
        "predictions land in the low bins regardless of correctness. No "
        "routing decision thresholds on this number (the ML router has its "
        "own separate `CONFIDENCE_FLOOR = 0.45` on model probabilities).",
    ]
    if result["misclassified"]:
        lines += [
            "",
            f"Misclassified prompts ({len(result['misclassified'])}):",
            "",
        ]
        for m in result["misclassified"]:
            lines.append(f"- id {m['id']} ({m['difficulty']}): expected `{m['expected']}`, got `{m['predicted']}`")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="E1: routing accuracy eval (no provider calls).")
    parser.add_argument("--json", action="store_true", help="Print the raw result blob instead of tables.")
    parser.add_argument("--dataset", default=str(DATASET_PATH), help="Path to the labeled JSONL dataset.")
    args = parser.parse_args()

    result = run_routing_eval(Path(args.dataset))
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(render_markdown(result))


if __name__ == "__main__":
    main()
