import pytest

from app.core.scorer import (
    BASELINE_LATENCY_MS,
    COST_PER_1K_TOKENS,
    ModelStats,
    normalize_cost,
    normalize_latency,
    rank_models,
)
from app.providers.registry import PROVIDER_REGISTRY

CAPABILITY_ONLY_WEIGHTS = {
    "capability": 1.0,
    "success_rate": 0.0,
    "cost": 0.0,
    "latency": 0.0,
    "availability": 0.0,
}

AVAILABILITY_ONLY_WEIGHTS = {
    "capability": 0.0,
    "success_rate": 0.0,
    "cost": 0.0,
    "latency": 0.0,
    "availability": 1.0,
}

EQUAL_HISTORY_STATS = {
    name: ModelStats(
        provider_name=name,
        task_type="code",
        total_requests=10,
        successful_requests=10,
        avg_latency_ms=900.0,
    )
    for name in ("groq", "gemini", "deepseek")
}


def test_code_task_ranks_deepseek_highest_by_capability():
    ranked = rank_models(
        "code", ["groq", "gemini", "deepseek"], {}, CAPABILITY_ONLY_WEIGHTS
    )
    assert [r.provider_name for r in ranked] == ["deepseek", "groq", "gemini"]


def test_default_weights_balance_cost_and_latency_on_equal_stats():
    # Documented behavior: with DEFAULT weights, deepseek is the only paid
    # (cost_score 0) and slowest (latency_score 0) provider, so a free and
    # fast model beats it on equal stats even for "code".
    ranked = rank_models("code", ["groq", "gemini", "deepseek"], {})
    assert ranked[0].provider_name == "groq"


def test_qa_task_ranks_groq_highest():
    ranked = rank_models("qa", ["groq", "gemini", "deepseek"], {})
    assert ranked[0].provider_name == "groq"


def test_open_circuit_ranks_last_despite_best_score():
    stats = {
        "deepseek": ModelStats(
            provider_name="deepseek",
            task_type="code",
            total_requests=10,
            successful_requests=10,
            avg_latency_ms=100.0,
            circuit_state="OPEN",
        ),
        "groq": ModelStats(
            provider_name="groq",
            task_type="code",
            total_requests=10,
            successful_requests=2,
            avg_latency_ms=800.0,
        ),
    }
    ranked = rank_models("code", ["deepseek", "groq", "gemini"], stats)
    assert ranked[-1].provider_name == "deepseek"
    assert ranked[-1].circuit_state == "OPEN"

    # Prove the override specifically: the identical provider without the
    # OPEN circuit would rank FIRST (best stats).
    healthy = {
        **stats,
        "deepseek": stats["deepseek"].model_copy(update={"circuit_state": "CLOSED"}),
    }
    ranked_healthy = rank_models("code", ["deepseek", "groq", "gemini"], healthy)
    assert ranked_healthy[0].provider_name == "deepseek"


def test_half_open_ranks_between_closed_and_open():
    providers = ["groq", "gemini", "deepseek"]
    circuits = {
        "groq": "CLOSED",
        "gemini": "HALF_OPEN",
        "deepseek": "OPEN",
    }
    stats = {
        name: ModelStats(
            provider_name=name,
            task_type="code",
            total_requests=10,
            successful_requests=10,
            avg_latency_ms=900.0,
            circuit_state=circuits[name],
        )
        for name in providers
    }

    # Pure availability ordering, all else truly equal:
    ranked = rank_models("general", providers, stats, AVAILABILITY_ONLY_WEIGHTS)
    assert [r.provider_name for r in ranked] == ["groq", "gemini", "deepseek"]
    assert [r.score for r in ranked] == [1.0, 0.5, 0.0]

    # And in realistic default-weight ranking, HALF_OPEN still sits between:
    ranked_realistic = rank_models("general", providers, stats)
    assert ranked_realistic[0].provider_name == "groq"
    assert ranked_realistic[-1].provider_name == "deepseek"


def test_confidence_score_formula_exact():
    stats = {
        "groq": ModelStats(
            provider_name="groq",
            task_type="summarization",
            total_requests=3,
            successful_requests=2,
        )
    }
    ranked = rank_models("summarization", ["groq"], stats)
    # groq capability for "summarization" = 0.6, success_rate = 2/3 → 0.4
    assert ranked[0].confidence_score == 0.4


def test_brand_new_provider_gets_optimistic_default():
    fresh = ModelStats(provider_name="deepseek", task_type="code")
    assert fresh.success_rate == 0.75

    # Empty stats dict must not crash and must use the optimistic default.
    ranked = rank_models("code", ["deepseek"], {})
    deepseek = next(r for r in ranked if r.provider_name == "deepseek")
    assert deepseek.confidence_score == round(0.75 * 1.0, 3)
    assert deepseek.circuit_state == "CLOSED"
    assert deepseek.score == pytest.approx(0.675)


def test_normalizers_return_unit_range_for_all_registered_providers():
    for provider_name in PROVIDER_REGISTRY:
        assert 0.0 <= normalize_cost(provider_name) <= 1.0
        assert 0.0 <= normalize_latency(provider_name) <= 1.0
        # Even with pathological historical latency, the result stays in range.
        assert 0.0 <= normalize_latency(provider_name, actual_avg_latency=100000.0) <= 1.0


def test_specific_normalizer_values():
    assert normalize_cost("groq") == 0.0
    assert normalize_cost("deepseek") == 1.0
    assert normalize_latency("deepseek") == 1.0
    assert normalize_latency("groq") == pytest.approx(300 / 900)
    assert normalize_latency("deepseek", actual_avg_latency=450.0) == pytest.approx(0.5)


def test_empty_provider_list_and_unknown_task_type():
    assert rank_models("code", [], {}) == []

    ranked = rank_models("creative_writing", ["groq", "gemini"], {})
    # Unmapped task type → every provider falls back to 0.3 capability
    assert all(
        r.confidence_score == round(0.75 * 0.3, 3) for r in ranked
    )
