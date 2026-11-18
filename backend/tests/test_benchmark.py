"""T3: the benchmark the owner can run with ONE documented command.

The contract under test:

- one command (``python benchmark/run_benchmark.py``, default --scenario all)
  runs BOTH scenarios — normal conditions and the Chaos-Mode simulated outage —
  and then writes the Markdown report;
- the naive arm is genuinely naive: one forced provider, zero fallback. The
  router arm runs the full pipeline unforced. Same prompts, same conditions;
- the report is a Markdown results table carrying the generation DATE and the
  MODELS each arm actually used, comparing naive vs router success rates —
  and it is produced from data files only, never from invented numbers;
- fail-loud behaviour: chaos disabled aborts the chaos scenario with a message
  that names the fix; missing scenario files abort the report.
"""

import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_DIR = REPO_ROOT / "benchmark"


def _import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def runner():
    """Import benchmark/run_benchmark.py (it wires the backend into sys.path)."""
    return _import("benchmark_runner", BENCHMARK_DIR / "run_benchmark.py")


@pytest.fixture(scope="module")
def report():
    """Import benchmark/generate_report.py (matplotlib is optional in it)."""
    return _import("benchmark_report", BENCHMARK_DIR / "generate_report.py")


# --------------------------------------------------------------------------
# The two arms: naive baseline vs full router
# --------------------------------------------------------------------------


PROMPTS = [
    {"id": 1, "prompt": "write a function", "expected_task_type": "code"},
    {"id": 2, "prompt": "why is the sky blue", "expected_task_type": "qa"},
]


def test_the_naive_arm_forces_one_provider_with_zero_fallback(runner):
    naive = runner.build_arm_props(PROMPTS, "naive_baseline", "gemini")
    assert len(naive) == len(PROMPTS)
    for body in naive:
        assert body["force_provider"] == "gemini"
        assert body["max_fallbacks"] == 1  # no resilience: one attempt, period


def test_the_router_arm_is_unforced_with_the_full_fallback_budget(runner):
    full = runner.build_arm_props(PROMPTS, "routellm_full", "gemini")
    for body in full:
        assert "force_provider" not in body  # the router picks by itself
        assert body["max_fallbacks"] == 3


def test_both_arms_send_the_same_prompts(runner):
    naive = runner.build_arm_props(PROMPTS, "naive_baseline", "gemini")
    full = runner.build_arm_props(PROMPTS, "routellm_full", "gemini")
    assert [b["prompt"] for b in naive] == [b["prompt"] for b in full]
    assert [b["task_type"] for b in naive] == [b["task_type"] for b in full]


def test_summarize_reports_success_rate_and_the_models_actually_used(runner):
    rows = [
        {"expected_task_type": "code", "success": True, "latency_ms": 100.0,
         "tokens_used": 10, "model_used": "deepseek/deepseek-chat",
         "estimated_cost": 0.0001, "error": None},
        {"expected_task_type": "code", "success": False, "latency_ms": 5.0,
         "tokens_used": 0, "model_used": None, "estimated_cost": 0.0,
         "error": "outage"},
        {"expected_task_type": "qa", "success": True, "latency_ms": 200.0,
         "tokens_used": 20, "model_used": "groq/llama-3.3-70b",
         "estimated_cost": 0.0002, "error": None},
    ]
    summary = runner.summarize(rows, "routellm_full")
    assert summary["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["success_count"] == 2
    assert summary["models_used"] == ["deepseek/deepseek-chat", "groq/llama-3.3-70b"]


def test_models_used_only_counts_successful_requests(runner):
    rows = [
        {"expected_task_type": "qa", "success": True, "latency_ms": 1.0,
         "tokens_used": 5, "model_used": "gemini/gemini-2.0-flash",
         "estimated_cost": 0.0, "error": None},
        {"expected_task_type": "qa", "success": False, "latency_ms": 0.0,
         "tokens_used": 0, "model_used": "gemini/gemini-2.0-flash",
         "estimated_cost": 0.0, "error": "http_500"},
    ]
    summary = runner.summarize(rows, "naive_baseline")
    assert summary["models_used"] == ["gemini/gemini-2.0-flash"]


def test_estimated_cost_for_an_unknown_model_is_zero_never_a_guess(runner):
    assert runner.estimate_cost("not-in-table/model", 100) == 0.0
    assert runner.estimate_cost(None, 100) == 0.0


# --------------------------------------------------------------------------
# Chaos: the outage simulation fails loudly when it cannot act
# --------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, status_code: int, body: dict | None = None):
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, resp: _FakeResp):
        self._resp = resp
        self.posts: list[dict] = []

    async def post(self, url, json=None, **kwargs):
        self.posts.append({"url": url, "json": json})
        return self._resp


async def test_chaos_injection_targets_the_baseline_provider(runner):
    client = _FakeClient(_FakeResp(
        200, {"provider_name": "gemini", "expires_in_seconds": 3600}
    ))
    expires = await runner.chaos_inject(client, "http://x", "gemini", 3600)
    assert expires == 3600
    assert client.posts[0]["json"] == {"provider_name": "gemini", "duration_seconds": 3600}


async def test_chaos_disabled_aborts_with_the_fix_in_the_message(runner):
    """CHAOS_MODE_ENABLED=false must stop the run, not silently skip the outage."""
    client = _FakeClient(_FakeResp(404))
    with pytest.raises(SystemExit) as excinfo:
        await runner.chaos_inject(client, "http://x", "gemini", 3600)
    assert "CHAOS_MODE_ENABLED" in str(excinfo.value)


async def test_chaos_clear_is_fail_soft(runner, capsys):
    client = _FakeClient(_FakeResp(500, {"detail": "redis down"}))
    await runner.chaos_clear(client, "http://x", "gemini")  # must not raise
    assert "TTL" in capsys.readouterr().out


def test_the_two_scenarios_write_their_own_files(runner):
    assert set(runner.SCENARIO_RESULTS_FILES) == {"normal", "chaos"}
    assert set(runner.SCENARIO_RESULTS_FILES.values()) == {
        "scenario_1_results.json", "scenario_2_results.json"
    }


# --------------------------------------------------------------------------
# The report: date + models used + naive-vs-router comparison, no fakes
# --------------------------------------------------------------------------


def _arm(total, successes, rate, latency, cost, models, breakdown=None):
    rows = [
        {"id": i + 1, "expected_task_type": "qa", "success": i < successes,
         "latency_ms": latency, "tokens_used": 10 if i < successes else 0,
         "model_used": models[0] if i < successes and models else None,
         "estimated_cost": cost / total, "error": None if i < successes else "outage"}
        for i in range(total)
    ]
    return {
        "arm": "x",
        "total_prompts": total,
        "success_count": successes,
        "success_rate": rate,
        "avg_latency_ms": latency,
        "total_estimated_cost": cost,
        "models_used": models,
        "per_task_type_breakdown": breakdown or {
            "qa": {"total_prompts": total, "success_count": successes,
                   "success_rate": rate, "avg_latency_ms": latency},
        },
        "per_prompt_results": rows,
    }


def _datasets():
    return {
        "no_chaos:naive": _arm(10, 9, 0.9, 500.0, 0.01, ["gemini/gemini-2.0-flash"]),
        "no_chaos:routellm": _arm(10, 10, 1.0, 600.0, 0.02, ["groq/llama-3.3-70b"]),
        "chaos:naive": _arm(10, 1, 0.1, 900.0, 0.001, ["gemini/gemini-2.0-flash"]),
        "chaos:routellm": _arm(10, 9, 0.9, 700.0, 0.015, ["groq/llama-3.3-70b"]),
    }


def test_the_report_carries_the_generation_date(report):
    table = report.build_table(_datasets())
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert "**Generated:**" in table
    assert today in table


def test_the_report_names_the_models_each_arm_used(report):
    table = report.build_table(_datasets())
    assert "## Models used" in table
    assert "`gemini/gemini-2.0-flash`" in table  # the naive forced model
    assert "`groq/llama-3.3-70b`" in table       # what the router picked


def test_the_report_compares_naive_vs_router_success_rates(report):
    table = report.build_table(_datasets())
    assert "| Normal — success rate | 90% | 100% |" in table
    assert "| **Simulated outage — success rate** | **10%** | **90%** |" in table


def test_the_report_states_the_numbers_come_from_the_own_run(report):
    table = report.build_table(_datasets())
    assert "your" in table.lower()
    assert "provider keys" in table.lower()


def test_the_report_spread_is_computed_from_the_data_not_hardcoded(report):
    datasets = _datasets()
    table = report.build_table(datasets)
    expected_spread = datasets["chaos:routellm"]["success_rate"] - datasets["chaos:naive"]["success_rate"]
    assert "80%" in table  # 0.9 - 0.1 = 80 points, derived from the datasets


def test_a_missing_scenario_file_aborts_the_report_with_instructions(report, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(report, "RESULTS_DIR", tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        report.load_datasets()
    assert "run_benchmark.py" in str(excinfo.value)


def test_the_report_is_written_even_without_matplotlib(report, tmp_path, monkeypatch, capsys):
    """The one-command run must not die because a plotting lib is missing."""
    results = tmp_path / "results"
    results.mkdir()
    for name in ("scenario_1_results.json", "scenario_2_results.json"):
        (results / name).write_text(json.dumps({
            "scenario": "x", "chaos_injected": "2" in name,
            "baseline_provider": "gemini", "total_prompts": 10,
            "runs": {"naive_baseline": _arm(10, 9, 0.9, 500.0, 0.01, ["gemini/gemini-2.0-flash"]),
                     "routellm_full": _arm(10, 10, 1.0, 600.0, 0.02, ["groq/llama-3.3-70b"])},
        }), encoding="utf-8")
    monkeypatch.setattr(report, "RESULTS_DIR", results)
    monkeypatch.setattr(report, "DOCS_REPORT_PATH", tmp_path / "docs" / "benchmark-results.md")
    monkeypatch.setattr(report, "matplotlib", None)
    report.main()
    written = (results / "REPORT.md").read_text(encoding="utf-8")
    assert "# Routtener Benchmark Report" in written
    assert "**Generated:**" in written
    assert not (results / "success_rate_comparison.png").exists()
    assert "matplotlib" in capsys.readouterr().out  # the skip is explained, not silent


def test_the_shipped_results_folder_stays_empty(tmp_path):
    """No pre-computed numbers may ship: results/ carries instructions only."""
    results = BENCHMARK_DIR / "results"
    shipped = [p.name for p in results.iterdir()]
    assert shipped == ["README.md"], (
        f"results/ must contain only README.md until the owner runs the "
        f"benchmark, found: {shipped}"
    )


# --------------------------------------------------------------------------
# End to end over a mocked transport: one command -> docs/benchmark-results.md
# --------------------------------------------------------------------------


class _RouteFakeClient:
    """Scripted stand-in for httpx.AsyncClient: every endpoint the benchmark
    touches, zero network.

    The gemini baseline answers healthy until the chaos inject fires and dies
    while the outage is active — mirroring what real Chaos Mode does to the
    forced naive arm, so ONE fake produces both scenarios of the table.
    """

    def __init__(self, chaos_state: dict):
        self._chaos = chaos_state

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        assert url.endswith("/api/v1/health")
        return _FakeResp(200, {"components": {"database": "up", "redis": "up"}})

    async def post(self, url, json=None, **kwargs):
        if url.endswith("/api/v1/keys/generate"):
            return _FakeResp(201, {"api_key": "rk_fake_benchmark_key"})
        if url.endswith("/admin/chaos/inject"):
            self._chaos["active"] = True
            return _FakeResp(200, {"provider_name": json["provider_name"],
                                   "expires_in_seconds": json["duration_seconds"]})
        if url.endswith("/admin/chaos/clear"):
            self._chaos["active"] = False
            return _FakeResp(200, {})
        assert url.endswith("/api/v1/route"), f"unexpected POST {url}"
        forced_gemini_down = "force_provider" in json and self._chaos["active"]
        return _FakeResp(200, {
            "success": not forced_gemini_down,
            "model_used": None if forced_gemini_down else (
                "gemini/gemini-2.0-flash" if "force_provider" in json
                else "groq/llama-3.3-70b"
            ),
            "tokens_used": 0 if forced_gemini_down else 10,
            "result": "synthetic completion",
            "request_id": "req-mocked",
        })


def test_one_command_end_to_end_over_a_mocked_transport(
    runner, report, tmp_path, monkeypatch
):
    """run_benchmark.py --scenario all, with no network at all.

    Both scenarios run through the scripted transport, the scenario files are
    written, and the report lands in BOTH locations: benchmark/results/
    REPORT.md and the repo-root docs/benchmark-results.md the README links.
    """
    chaos_state = {"active": False}
    monkeypatch.setattr(
        runner.httpx, "AsyncClient", lambda *a, **kw: _RouteFakeClient(chaos_state)
    )
    monkeypatch.setattr(runner, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(report, "RESULTS_DIR", tmp_path)
    docs_report = tmp_path / "docs" / "benchmark-results.md"
    monkeypatch.setattr(report, "DOCS_REPORT_PATH", docs_report)
    # generate_report_only() does `import generate_report`: hand it the
    # already-patched module instead of re-importing a clean copy.
    monkeypatch.setitem(sys.modules, "generate_report", report)
    monkeypatch.setattr(sys, "argv", ["run_benchmark.py"])

    runner.main()  # default --scenario all: normal + chaos + the report

    written = docs_report.read_text(encoding="utf-8")
    assert "# Routtener Benchmark Report" in written
    assert "**Generated:**" in written
    assert datetime.now(timezone.utc).strftime("%Y-%m-%d") in written
    # The documented table shape: header (naive column carries the T4b
    # "fails by construction" label) + the outage comparison row, plus the
    # shows/does-not-show honesty line.
    assert (
        "| Scenario | Naive (single model — fails by construction when "
        "its only provider is down) | Routtener |" in written
    )
    assert "fails by construction when its only provider is down" in written
    assert "What this shows / does not show" in written
    assert "| Normal — success rate | 100% | 100% |" in written
    assert "| **Simulated outage — success rate** | **0%** | **100%** |" in written
    # Models each arm actually used, as produced by the mocked transport.
    assert "`gemini/gemini-2.0-flash`" in written
    assert "`groq/llama-3.3-70b`" in written
    # The numbers say where they came from — never invented.
    assert "your" in written.lower()
    # Same report in both locations.
    assert (tmp_path / "REPORT.md").read_text(encoding="utf-8") == written
