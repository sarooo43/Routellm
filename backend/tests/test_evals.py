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

import asyncio
import importlib.util
import json
import os
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


@pytest.fixture(scope="module")
def evals_harness():
    return _load("evals_harness", "harness.py")


@pytest.fixture(autouse=True)
def _restore_structlog_after_each_test(evals_harness):
    """The eval scripts silence the app's structlog globally; make sure no
    test leaves it silenced for the rest of the suite (regression seen in
    CI: tests/unit/test_local.py captures structlog stdout)."""
    yield
    evals_harness.restore_structlog()


# --------------------------------------------------------------------------
# T4a: wilson_interval against known closed-form values
# --------------------------------------------------------------------------


def test_wilson_50_of_50_lower_bound_is_92_9_not_70_3(evals_harness):
    """The observed bug: a 50/50 run printed [70.3%, 100%]. The correct
    95% Wilson lower bound for p_hat = 1 is n / (n + z^2)."""
    lo, hi = evals_harness.wilson_interval(50, 50)
    assert lo == pytest.approx(50 / (50 + evals_harness.Z_95**2), abs=1e-9)
    assert lo == pytest.approx(0.9287, abs=1e-3)
    assert hi == pytest.approx(1.0)


def test_wilson_0_of_50(evals_harness):
    lo, hi = evals_harness.wilson_interval(0, 50)
    assert lo <= 1e-12  # 0 modulo float rounding
    # Closed form for p_hat = 0: upper = z^2 / (n + z^2).
    assert hi == pytest.approx(
        evals_harness.Z_95**2 / (50 + evals_harness.Z_95**2), abs=1e-9
    )


def test_wilson_5_of_10(evals_harness):
    lo, hi = evals_harness.wilson_interval(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3)
    assert hi == pytest.approx(0.7634, abs=1e-3)
    assert lo < 0.5 < hi


def test_wilson_1_of_1(evals_harness):
    lo, hi = evals_harness.wilson_interval(1, 1)
    # For p_hat = 1: lower = 1 / (1 + z^2) = 0.2065, upper clamped to 1.
    assert lo == pytest.approx(1 / (1 + evals_harness.Z_95**2), abs=1e-9)
    assert hi == pytest.approx(1.0)


def test_wilson_0_of_0_edge_case(evals_harness):
    assert evals_harness.wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_interval_never_exceeds_the_point_estimate_span(evals_harness):
    """Sanity: the interval contains p_hat and shrinks as n grows."""
    for successes, n in ((3, 10), (37, 50), (99, 100), (12, 200)):
        p_hat = successes / n
        lo, hi = evals_harness.wilson_interval(successes, n)
        assert lo <= p_hat <= hi or (successes == 0 or successes == n)
        assert 0.0 <= lo <= hi <= 1.0
    narrow = evals_harness.wilson_interval(45, 50)
    wide = evals_harness.wilson_interval(9, 10)
    assert (narrow[1] - narrow[0]) < (wide[1] - wide[0])


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
    # T4a/baseline-flake fix: with n=10 the delta can legitimately be a few
    # milliseconds negative from wall-clock jitter alone (observed -6.2ms and
    # -0.8ms on identical code). Asserting >= 0 made this test fail randomly.
    # The MEANINGFUL invariant: routing overhead does not add orders of
    # magnitude - the auto p95 stays within a generous bound of the naive p95.
    assert result["overhead_delta"]["p95_ms"] < result["naive"]["p95_ms"]


# --------------------------------------------------------------------------
# E5: the report writer
# --------------------------------------------------------------------------


def test_run_evals_writes_honest_report(evals_runner, monkeypatch, tmp_path):
    # T4c: a mocked run writes ONLY to the gitignored evals/results-mock/
    # path, never to docs/. DOC_PATH is monkeypatched to a sentinel to prove
    # a mocked run never touches it.
    mock_dir = tmp_path / "results-mock"
    doc_path = mock_dir / "eval-results.md"
    docs_sentinel = tmp_path / "docs-eval-results.md"
    monkeypatch.setattr(evals_runner, "MOCK_DIR", mock_dir)
    monkeypatch.setattr(evals_runner, "MOCK_REPORT_PATH", doc_path)
    monkeypatch.setattr(evals_runner, "DOC_PATH", docs_sentinel)
    test_argv = ["run_evals.py", "--n", "2", "--n-overhead", "5"]
    monkeypatch.setattr(sys, "argv", test_argv)
    evals_runner.main()

    assert doc_path.exists()
    assert not docs_sentinel.exists()
    text = " ".join(doc_path.read_text(encoding="utf-8").split())
    # the report must carry the provenance + honesty contract
    for fragment in (
        "Commit:",
        "What this does not show",
        "MODE: MOCKED",
        "not a measurement of any real provider",
        "Wilson 95%",
        "not measured: scripted stubs",
    ):
        assert fragment in text, f"report is missing {fragment!r}"
    # and must not pretend to be a real-provider result
    assert "NOT the real providers" in text


def test_no_committed_results_file_has_a_mocked_header():
    """T4c: a MOCKED results file must never be committed. The mocked suite
    writes to the gitignored evals/results-mock/; live results (docs/) carry
    a MODE: LIVE header, so any committed file with a MOCKED header is a
    regression."""
    offenders = []
    skip = {".git", "node_modules", ".venv", "dist", "__pycache__",
            ".pytest_cache", ".ruff_cache", "coverage"}
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        for name in files:
            if not name.endswith(".md"):
                continue
            path = Path(root) / name
            try:
                head = path.read_text(encoding="utf-8", errors="ignore")[:2000]
            except OSError:
                continue
            if "MODE: MOCKED" in head:
                offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == []

    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    active_rules = [
        line.strip()
        for line in gitignore.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert any("evals/results-mock" in rule for rule in active_rules)
    assert not any("docs/eval-results.md" in rule for rule in active_rules)


# --------------------------------------------------------------------------
# T4b: naive-arm labelling + first-vs-steady reporting
# --------------------------------------------------------------------------


def test_resilience_render_labels_the_naive_arm_and_what_it_shows(resilience_eval):
    result = resilience_eval.run_scenario_mocked("provider_down", 2, 3)
    text = resilience_eval.render_markdown([result], 2, "mocked (test)")
    assert "fails by construction when its only provider is down" in text
    assert "What this comparison shows / does not show" in text
    assert "FAILOVER WORKS" in text
    assert "First request vs steady state" in text
    # the naive arm must actually collapse in a provider-down scenario
    assert result["naive"]["successes"] == 0
    # and both arms carry first/steady breakdowns
    for arm_key in ("router", "naive"):
        arm = result[arm_key]
        assert set(arm["first_request"]) == {"attempts", "latency_ms"}
        assert set(arm["steady_state"]) == {"mean_attempts", "mean_latency_ms"}


# --------------------------------------------------------------------------
# T4d: E3 mocked mode reports no percentages
# --------------------------------------------------------------------------


def test_quality_mocked_render_prints_not_measured_and_no_percentages(quality_eval):
    result = quality_eval.run_quality_eval()
    assert result["mode"].startswith("mocked")
    render = quality_eval.render_markdown(result)
    assert "not measured: scripted stubs" in render
    assert "%" not in render
    # the blob keeps the numbers for machinery checks - only the RENDER hides
    # them (a live render with mode "live ..." prints percentages + n)
    assert 0.0 <= result["arms"][0]["accuracy"] <= 1.0


def test_quality_live_render_shows_percentages_with_n_per_kind(quality_eval):
    result = quality_eval.run_quality_eval()
    result = {**result, "mode": "live (real providers)"}
    render = quality_eval.render_markdown(result)
    assert "%" in render
    assert "n=" in render


# --------------------------------------------------------------------------
# T4h: live-mode safeguards (mocked transports - no network, no keys)
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data


class _FakeClient:
    """Minimal async client double for preflight_live: canned responses per
    (method, path); records every call so tests can assert no request was
    fired past the refusal point."""

    def __init__(self, routes):
        self._routes = routes
        self.calls = []

    async def get(self, path, **kwargs):
        self.calls.append(("GET", path))
        return self._routes[("GET", path)]

    async def post(self, path, **kwargs):
        self.calls.append(("POST", path))
        return self._routes[("POST", path)]


def _preflight_routes(*, configured, chaos_status):
    return {
        ("POST", "/api/v1/keys/generate"): _FakeResponse(200, {"api_key": "eval-key"}),
        ("GET", "/api/v1/provider-keys"): _FakeResponse(
            200,
            [
                {"provider_name": name, "configured": True}
                for name in configured
            ]
            + [
                {"provider_name": name, "configured": False}
                for name in ("groq", "gemini", "deepseek")
                if name not in configured
            ],
        ),
        ("GET", "/api/v1/admin/chaos/status"): _FakeResponse(
            chaos_status, {"active_chaos": []}
        ),
    }


def test_live_refuses_without_yes_spend_quota(resilience_eval, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["run_resilience_eval.py", "--live"])
    with pytest.raises(SystemExit) as exc:
        resilience_eval.main()
    assert "--yes-spend-quota" in str(exc.value)


def test_preflight_aborts_with_fewer_than_two_configured_providers(resilience_eval):
    client = _FakeClient(_preflight_routes(configured=["groq"], chaos_status=200))
    with pytest.raises(SystemExit) as exc:
        asyncio.run(resilience_eval.preflight_live(client, 5))
    assert "fewer than 2 providers" in str(exc.value)
    # nothing past the key-status check was attempted
    assert ("GET", "/api/v1/admin/chaos/status") not in client.calls


def test_preflight_refuses_chaos_mode_when_backend_disables_it(resilience_eval):
    client = _FakeClient(
        _preflight_routes(configured=["groq", "deepseek"], chaos_status=404)
    )
    with pytest.raises(SystemExit) as exc:
        asyncio.run(resilience_eval.preflight_live(client, 5))
    assert "CHAOS_MODE_ENABLED" in str(exc.value)


def test_preflight_passes_and_prints_budget_and_providers(resilience_eval, capsys):
    client = _FakeClient(
        _preflight_routes(configured=["groq", "deepseek", "gemini"], chaos_status=200)
    )
    key = asyncio.run(resilience_eval.preflight_live(client, 7))
    assert key == "eval-key"
    out = capsys.readouterr().out
    assert "Request budget: 7 requests x 1 scenario" in out
    assert "groq, deepseek, gemini" in out


# --------------------------------------------------------------------------
# T4j: eval helpers must not leave global logging state changed
# --------------------------------------------------------------------------


def test_silence_and_restore_leave_global_logging_unchanged(evals_harness):
    import logging

    import structlog

    before_cfg = structlog.get_config()
    before_level = logging.getLogger().getEffectiveLevel()
    evals_harness.silence_structlog()
    assert logging.getLogger().getEffectiveLevel() >= logging.CRITICAL
    evals_harness.restore_structlog()
    assert structlog.get_config() == before_cfg
    assert logging.getLogger().getEffectiveLevel() == before_level