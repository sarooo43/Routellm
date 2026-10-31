"""Part B of the ML routing phase — train the lightweight router classifier.

Loads raw_responses.jsonl (from scripts/collect_training_data.py), derives
per-prompt best-provider labels, embeds prompts with sentence-transformers
("all-MiniLM-L6-v2", small + free + local), trains a scikit-learn
LogisticRegression on (embedding → best_provider), and saves:

    backend/app/ml_models/router_classifier.pkl   (joblib artifact dict)
    {"classifier": <sklearn model>, "embedder": <SentenceTransformer>,
     "embedder_source": "all-MiniLM-L6-v2", "classes": [...], ...}

Requires backend/requirements-ml.txt (sentence-transformers, scikit-learn,
joblib, numpy). Embeddings are computed locally; the only API cost is the
judge calls made earlier during data collection.

HONESTY: with ~37-150 judge-labeled examples this model will rarely beat a
coin toss by much — the script PRINTS the majority-class baseline and asks
you to compare before believing the accuracy number. See README.
"""

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure") and (
    sys.stdout.encoding or ""
).lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.core.scorer import COST_PER_1K_TOKENS  # noqa: E402

IN_FILE = Path(__file__).resolve().parent / "training_data" / "raw_responses.jsonl"
OUT_DIR = BACKEND_DIR / "app" / "ml_models"
OUT_FILE = OUT_DIR / "router_classifier.pkl"

EMBEDDER_NAME = "all-MiniLM-L6-v2"
RANDOM_SEED = 42


def load_rows(in_file: Path) -> list[dict]:
    if not in_file.exists():
        raise SystemExit(
            f"FATAL: {in_file} not found. Run collect_training_data.py first:\n"
            "  backend/.venv/Scripts/python.exe scripts/collect_training_data.py"
        )
    rows = []
    for line in in_file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    if not rows:
        raise SystemExit(f"FATAL: {in_file} is empty — nothing to train on.")
    return rows


def best_provider_for_prompt(triples: list[dict]) -> str:
    """Label = highest judge score; ties break by cost then latency."""
    successful = [t for t in triples if t["success"]]
    if not successful:
        return None
    return max(
        successful,
        key=lambda t: (
            t["quality_score"],
            -COST_PER_1K_TOKENS.get(t["provider"], 0.0),
            -t["latency_ms"],
        ),
    )["provider"]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train the router classifier on judge-scored data."
    )
    parser.add_argument("--in-file", type=Path, default=IN_FILE)
    parser.add_argument(
        "--out-file", type=Path, default=OUT_FILE,
        help="Where to save the joblib artifact the app will load.",
    )
    args = parser.parse_args()

    # Heavy ML deps imported inside main so --help stays dependency-free.
    try:
        import joblib
        import numpy
        from sentence_transformers import SentenceTransformer
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import classification_report
        from sklearn.model_selection import train_test_split
    except ImportError as e:
        raise SystemExit(
            f"FATAL: missing ML dependency ({e}). Install backend/"
            "requirements-ml.txt first:\n"
            "  backend/.venv/Scripts/python.exe -m pip install -r "
            "backend/requirements-ml.txt"
        )

    rows = load_rows(args.in_file)
    total_rows = len(rows)
    failed_rows = sum(1 for r in rows if not r["success"])

    # Labeled dataset: best provider per prompt (deterministic tie-break).
    by_prompt: dict[str, list[dict]] = {}
    for row in rows:
        by_prompt.setdefault(row["prompt"], []).append(row)

    labeled: list[tuple[str, str]] = []
    skipped = 0
    for prompt, triples in sorted(by_prompt.items()):
        label = best_provider_for_prompt(triples)
        if label is None:
            skipped += 1  # every provider failed for this prompt
        else:
            labeled.append((prompt, label))

    if not labeled:
        raise SystemExit(
            "FATAL: no trainable prompts — all provider calls failed "
            "(check API keys in backend/.env) or the dataset is empty."
        )

    prompts, labels = map(list, zip(*labeled))
    label_counts = Counter(labels)

    print("=" * 62)
    print("Data summary (honesty numbers BEFORE training)")
    print("-" * 62)
    print(f"rows collected:        {total_rows} ({failed_rows} failed provider calls)")
    print(f"trainable prompts:     {len(prompts)}"
          f"{f' ({skipped} skipped: all providers failed)' if skipped else ''}")
    print(f"class distribution:    {dict(label_counts.most_common())}")
    majority_label, majority_count = label_counts.most_common(1)[0]
    majority_baseline = majority_count / len(labels)
    print(
        f"majority-class baseline: {majority_baseline:.1%} "
        f"(always predicting '{majority_label}') — your model MUST beat this "
        "to claim it learned anything"
    )
    if len(label_counts) < 2:
        raise SystemExit(
            "FATAL: only one provider class present — collect data with at "
            "least two providers before training."
        )

    print(f"\nembedding {len(prompts)} prompts with '{EMBEDDER_NAME}'...")
    embedder = SentenceTransformer(EMBEDDER_NAME)
    vectors = numpy.asarray(embedder.encode(prompts, show_progress_bar=False))

    # 80/20 split; stratify only when classes are big enough for it.
    stratify = labels if min(label_counts.values()) >= 5 else None
    if stratify is None:
        print("NOTE: some class has <5 examples — cannot stratify the split.")
    x_train, x_test, y_train, y_test = train_test_split(
        vectors, labels, test_size=0.2, random_state=RANDOM_SEED, stratify=stratify
    )

    print(f"training LogisticRegression on {len(x_train)} train / "
          f"{len(x_test)} test examples...")
    model = LogisticRegression(max_iter=1000, random_state=RANDOM_SEED)
    model.fit(x_train, y_train)
    accuracy = model.score(x_test, y_test)

    print()
    print("=" * 62)
    print("RESULTS — read these with small-dataset skepticism")
    print("=" * 62)
    print(f"test accuracy:            {accuracy:.1%}  "
          f"({round(accuracy * len(y_test))}/{len(y_test)} correct)")
    print(f"majority-class baseline:  {majority_baseline:.1%}")
    delta = accuracy - majority_baseline
    verdict = "beats" if delta > 0 else ("matches" if delta == 0 else "fails to beat")
    print(f"vs baseline:              "
          f"{verdict} it by {delta:+.1%} → {'learning signal present' if delta > 0 else 'likely NO real learning signal — honest conclusion: dataset too small/imbalanced'}")
    print()
    print(classification_report(
        y_test, model.predict(x_test), zero_division=0,
    ))

    # Serve-time artifact: pickle BOTH the model and embedder so the app is
    # self-contained (embedding needs no download at serving).
    args.out_file.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        # Small physical model that fits the router's real constraints.
        "classifier": model,
        "model_kind": "LogisticRegression(minilm-l6-embeddings)",
        "embedder": embedder,
        "embedder_source": EMBEDDER_NAME,
        "classes": list(model.classes_),
        "metrics": {
            "test_accuracy": float(accuracy),
            "majority_baseline": float(majority_baseline),
            "train_examples": len(x_train),
            "test_examples": len(x_test),
        },
        "trained_at": datetime.now(timezone.utc).isoformat(),
    }
    joblib.dump(artifact, args.out_file)
    print(f"saved {args.out_file}")
    print(
        "\nnext: set ENABLE_ML_ROUTING=true in backend/.env to blend these "
        "predictions into routing (60/40 over the rule-based table). The "
        "system runs rule-based-only without the model, so this is optional."
    )


if __name__ == "__main__":
    main()
