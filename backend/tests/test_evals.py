"""Contracts for the eval suite in ``evals/``.

The eval scripts are repo-root tools, so they are loaded here via importlib
(mirroring test_run_launcher.py) and exercised through their public run
functions with SMALL n - the full-size numbers belong to the CLI runs, not
to CI. Everything is mocked transport: no network, no keys, no real Redis.

Pinned here:
* output SHAPE of E1-E4 (fields present, values in range, zero provider calls),
* DETERMINISM (same input -> same successes/attempts; latencies jitter),
* the report writer produces docs/eval-results.md with the honest framing,
* the MUTATION check: breaking the router's fallback (max_fallbacks=1) drops
  the resilience eval's success rate from 100% to 0%.
"""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
EVALS_DIR = REPO_ROOT / "evals"


def _load(module_name: str, file_name: str):
    """Import an eval script as a module without going through sys.path tricks."""
    spec = importlib.util.spec_from_file_location(module_name, EVALS_DIR / file_name)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def routing_eval():
    return _load("evals_run_routing_eval", "run_routing_eval.py")


@pytest.fixture(scope="module")
def resilience_eval():
    return _load("evals_run_resilience_eval", "run_resilience_eval.py")


@pytest.fixture(scope="module")
def quality_eval():
    return _load("evals_run_quality_eval", "run_quality_eval.py")


@pytest.fixture(scope="module")
def overhead_eval():
    return _load("evals_run_overhead_eval", "run_overhead_eval.py")


@pytest.fixture(scope="module")
def evals_runner():
    return _load("evals_run_evals", "run_evals.py")


# --------------------------------------------------------------------------
# E1: routing accuracy
# --------------------------------------------------------------------------


def test_routing_eval_output_shape(routing_eval):
    result = routing_eval.run_routing_eval()
    assert result["eval"] == "routing_accuracy"
    assert result["n"] >= 100
    assert 0.0 <= result["accuracy"] <= 1.0
    assert result["provider_calls"] == 0, "E1 must never touch a provider"
    # confusion matrix: every row totals to that class's support; grand total n
    labels = set(result["confusion_matrix"])
    total = sum(sum(row.values()) for row in result["confusion_matrix"].values())
    assert total == result["n"]
    assert set(result["per_class"]) == labels
    for stats in result["per_class"].values():
        assert set(stats) == {"support", "recall", "precision"}
    # calibration bins: 4 reliability bins, each fully accounted for
    bins = result["calibration_bins"]
    assert len(bins) == 4
    assert sum(b["n"] for b in bins) == result["n"]
    for b in bins:
        assert b["n"] == 0 or (0.0 <= b["accuracy"] <= 1.0 and 0.0 < b["mean_confidence"] <= 1.0)


def test_routing_eval_is_deterministic(routing_eval):
    first = routing_eval.run_routing_eval()
    second = routing_eval.run_routing_eval()
    assert first == second


def test_routing_eval_fails_loudly_on_duplicate_prompts(routing_eval, tmp_path):
    dataset = tmp_path / "bad.jsonl"
    row = {"id": 1, "prompt": "Hello there", "expected_task_type": "general", "difficulty": "easy"}
    row2 = dict(row, id=2)
    dataset.write_text(
        json.dumps(row) + "\n" + json.dumps(row2) + "\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit, match="duplicate"):
        routing_eval.load_labeled_prompts(dataset)


# --------------------------------------------------------------------------
# E2: resilience
# --------------------------------------------------------------------------


def test_resilience_eval_output_shape(resilience_eval):
    result = resilience_eval.run_scenario_mocked("provider_down", 5, 3)
    for arm_key in ("router", "naive"):
        arm = result[arm_key]
        assert arm["n"] == 5
        assert 0.0 <= arm["success_rate"] <= 1.0
        lo, hi = arm["wilson_95"]
        assert 0.0 <= lo <= arm["success_rate"] <= hi <= 1.0
        assert arm["mean_attempts"] >= 1.0
    assert result["router"]["successes"] == 5, "router must survive a provider outage"
    assert result["naive"]["successes"] == 0, "naive baseline has no fallback"


def test_resilience_eval_is_deterministic(resilience_eval):
    first = resilience_eval.run_scenario_mocked("provider_down", 5, 3)
    second = resilience_eval.run_scenario_mocked("provider_down", 5, 3)
    # latencies are wall-clock and jitter; successes and attempts do not
    for arm_key in ("router", "naive"):
        assert first[arm_key]["successes"] == second[arm_key]["successes"]
        assert first[arm_key]["mean_attempts"] == second[arm_key]["mean_attempts"]


def test_resilience_eval_covers_all_five_scenarios(resilience_eval):
    assert set(resilience_eval.SCENARIOS) == {
        "provider_down",
        "rate_limited_429",
        "retired_model_404",
        "key_wide_403",
        "slow_response",
    }


def test_mutation_breaking_fallback_drops_success_rate(resilience_eval):
    """Mutation check: break the router's fallback and the eval must notice.

    max_fallbacks=1 means the router attempts ONLY the (scripted-failing)
    primary - the same broken behavior a developer could introduce by
    regressing the fallback loop. The success rate must collapse from 100%
    to 0%; if it does not, the eval is not measuring resilience.
    """
    healthy = resilience_eval.run_scenario_mocked("provider_down", 5, 3)
    broken = resilience_eval.run_scenario_mocked("provider_down", 5, 1)
    assert healthy["router"]["success_rate"] == 1.0
    assert broken["router"]["success_rate"] == 0.0


# --------------------------------------------------------------------------
# E3: quality
# --------------------------------------------------------------------------


def test_quality_eval_output_shape_and_routing_link(quality_eval):
    result = quality_eval.run_quality_eval()
    assert result["n_tasks"] >= 24
    arms = {arm["arm"]: arm for arm in result["arms"]}
    assert set(arms) == {"auto", "pinned_groq", "pinned_deepseek"}
    for arm in arms.values():
        assert 0.0 <= arm["accuracy"] <= 1.0
        lo, hi = arm["wilson_95"]
        assert 0.0 <= lo <= arm["accuracy"] <= hi <= 1.0
        assert set(arm["per_kind"]) == {"math", "code", "json"}
    # with zero provider stats the router ranks the fully-correct stub first,
    # so auto must match the best pinned arm and beat the weak pinned one
    assert arms["auto"]["accuracy"] == arms["pinned_groq"]["accuracy"]
    assert arms["pinned_deepseek"]["accuracy"] < arms["auto"]["accuracy"]


def test_quality_checkers_mechanical_only(quality_eval):
    # math: last number must match exactly
    assert quality_eval.check_math("bla 123. Answer: 42", 42)
    assert not quality_eval.check_math("bla 123. Answer: 43", 42)
    assert not quality_eval.check_math("no numbers at all", 42)
    # code: must exec and pass the bundled tests
    assert quality_eval.check_code("```python\ndef f(x):\n    return x * 2\n```", ["assert f(3) == 6"])
    assert not quality_eval.check_code("```python\ndef f(x):\n    return x + 1\n```", ["assert f(3) == 6"])
    assert not quality_eval.check_code("total garbage", ["assert f(3) == 6"])
    # json: schema validation without the jsonschema dependency
    schema = {"type": "object", "properties": {"a": {"type": "number"}}, "required": ["a"]}
    assert quality_eval.validate_against_schema({"a": 1}, schema) == []
    assert quality_eval.validate_against_schema({"a": "1"}, schema)
    assert quality_eval.validate_against_schema({}, schema)
    assert not quality_eval.check_json("not json at all", schema)


# --------------------------------------------------------------------------
# E4: overhead
# --------------------------------------------------------------------------


def test_overhead_eval_output_shape(overhead_eval):
    result = overhead_eval.run_overhead_eval(10)
    for arm_key in ("auto", "naive"):
        arm = result[arm_key]
        assert arm["n"] == 10
        assert 0 < arm["p50_ms"] <= arm["p95_ms"]
    assert result["overhead_delta"]["p50_ms"] >= 0.0
    assert result["overhead_delta"]["p95_ms"] >= 0.0


# --------------------------------------------------------------------------
# E5: the report writer
# --------------------------------------------------------------------------


def test_run_evals_writes_honest_report(evals_runner, monkeypatch, tmp_path):
    doc_path = tmp_path / "eval-results.md"
    monkeypatch.setattr(evals_runner, "DOC_PATH", doc_path)
    test_argv = ["run_evals.py", "--n", "2", "--n-overhead", "5"]
    monkeypatch.setattr(sys, "argv", test_argv)
    evals_runner.main()

    assert doc_path.exists()
    text = " ".join(doc_path.read_text(encoding="utf-8").split())
    # the report must carry the provenance + honesty contract
    for fragment in (
        "Commit:",
        "What this does not show",
        "MOCKED eval run",
        "Wilson 95%",
        "your own provider keys",
    ):
        assert fragment in text, f"report is missing {fragment!r}"
    # and must not pretend to be a real-provider result
    assert "NOT the real providers" in text