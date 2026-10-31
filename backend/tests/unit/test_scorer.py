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


# --------------------------------------------------------------------------
# Feedback blending (confidence-weighted) and its effect on ranking
# --------------------------------------------------------------------------


def test_zero_feedback_feedback_adjusted_rate_equals_raw_success_rate():
    stats = ModelStats(
        provider_name="groq",
        task_type="summarization",
        total_requests=10,
        successful_requests=9,  # raw 0.9
        positive_feedback_count=0,
        negative_feedback_count=0,
    )
    assert stats.feedback_adjusted_success_rate == stats.success_rate == 0.9


def test_small_feedback_sample_is_dampened_toward_raw_rate():
    # 2 ratings (1 positive, 1 negative → feedback_rate 0.5), raw rate 0.9.
    # weight = min(0.5, 2/20) = 0.1 → tiny shift toward feedback, NOT 50/50.
    stats = ModelStats(
        provider_name="groq",
        task_type="summarization",
        total_requests=10,
        successful_requests=9,
        positive_feedback_count=1,
        negative_feedback_count=1,
    )
    expected = (1 - 0.1) * 0.9 + 0.1 * 0.5
    assert stats.feedback_adjusted_success_rate == pytest.approx(expected)
    # The dampening actually matters: the blended value is much closer to
    # the raw rate than to the small-sample feedback rate.
    assert abs(stats.feedback_adjusted_success_rate - 0.9) < 0.04


def test_at_least_ten_ratings_hits_50_50_weight_cap_and_stays_there():
    # weight: min(0.5, total/20) — at exactly 10 ratings total/20 == 0.5.
    stats = ModelStats(
        provider_name="gemini",
        task_type="summarization",
        total_requests=10,
        successful_requests=10,  # raw 1.0
        positive_feedback_count=10,
        negative_feedback_count=10,  # feedback_rate 0.5, total 20 ≥ cap
    )
    expected = 0.5 * 1.0 + 0.5 * 0.5  # capped: exactly 50/50, not more
    assert stats.feedback_adjusted_success_rate == pytest.approx(expected)


def test_massive_negative_feedback_still_capped_at_half_influence():
    # 1000 ratings: weight still 0.5 (cap), so 100% negative feedback can
    # only pull an otherwise-perfect rate down to 0.5 — never fully zeros it.
    stats = ModelStats(
        provider_name="gemini",
        task_type="summarization",
        total_requests=100,
        successful_requests=100,
        positive_feedback_count=0,
        negative_feedback_count=1000,
    )
    assert stats.feedback_adjusted_success_rate == pytest.approx(0.5)


def test_feedback_ranks_provider_below_identical_peer_with_positive_feedback():
    """The core value proposition: feedback alone reorders the ranking.

    Two providers with IDENTICAL raw API stats and capability ("general"
    weights both gemini and groq at 0.9) — the only differing signal is
    human feedback. The negatively-rated one must rank lower even though
    its raw API success rate is perfect.
    """
    base = dict(
        task_type="general",
        total_requests=10,
        successful_requests=10,  # both raw success_rate = 1.0
        avg_latency_ms=100.0,
    )
    stats = {
        "gemini": ModelStats(
            provider_name="gemini",
            positive_feedback_count=10,
            negative_feedback_count=10,  # adjusted rate → 0.75
            **base,
        ),
        "groq": ModelStats(
            provider_name="groq",
            positive_feedback_count=20,
            negative_feedback_count=0,  # adjusted rate → 1.0
            **base,
        ),
    }
    ranked = rank_models("general", ["groq", "gemini"], stats)

    assert ranked[0].provider_name == "groq"
    assert ranked[1].provider_name == "gemini"
    assert ranked[0].score > ranked[1].score
    # And the negative-feedback provider's confidence is genuinely lower:
    assert ranked[1].confidence_score < ranked[0].confidence_score


def test_feedback_zero_totals_in_stats_dict_do_not_crash_ranking():
    ranked = rank_models("summarization", ["gemini"], {"gemini": ModelStats(
        provider_name="gemini", task_type="summarization"
    )})
    # No feedback + no history → optimistic default, exactly as before this phase.
    assert ranked[0].confidence_score == round(0.75 * 1.0, 3)


# --------------------------------------------------------------------------
# EXPERIMENTAL ML hybrid blending (rank_models + ml_classifier)
# --------------------------------------------------------------------------


class StubMLClassifier:
    """Minimal stand-in matching MLRouterClassifier's serving interface
    (including its real semantics: an unloaded model returns None)."""

    def __init__(self, probabilities: dict[str, float] | None, loaded: bool = True):
        self._probs = probabilities
        self.is_loaded = loaded

    def predict_best_provider(self, prompt: str):
        if not self.is_loaded:
            return None
        return self._probs


ML_OFF_STATS = {
    "groq": ModelStats(provider_name="groq", task_type="summarization"),
    "gemini": ModelStats(provider_name="gemini", task_type="summarization"),
}


def test_ml_classifier_none_produces_pure_rule_based_ranking(monkeypatch):
    """mark_models(prompt=..., ml_classifier=None) == rule-based behavior."""
    with_ml_off = rank_models("summarization", ["groq", "gemini"], ML_OFF_STATS)
    result = rank_models(
        "summarization", ["groq", "gemini"], ML_OFF_STATS,
        prompt="summarize this", ml_classifier=None,
    )
    assert [r.score for r in result] == [r.score for r in with_ml_off]
    assert [r.provider_name for r in result] == [r.provider_name for r in with_ml_off]


def test_flag_off_ignores_ml_classifier(monkeypatch):
    """ENABLE_ML_ROUTING=False: even a loaded classifier is ignored — the
    scores are bit-for-bit identical to rule-based-only."""
    monkeypatch.setattr("app.config.settings.ENABLE_ML_ROUTING", False)
    baseline = rank_models(
        "summarization", ["groq", "gemini"], ML_OFF_STATS,
        prompt="summarize this",
        ml_classifier=StubMLClassifier({"gemini": 0.9, "groq": 0.1}),
    )
    pure = rank_models("summarization", ["groq", "gemini"], ML_OFF_STATS)
    assert [r.score for r in baseline] == [r.score for r in pure]


def test_flag_on_blends_ml_prediction_60_40(monkeypatch):
    """ENABLE_ML_ROUTING=True: capability = 0.6*ml + 0.4*rule, per provider."""
    monkeypatch.setattr("app.config.settings.ENABLE_ML_ROUTING", True)

    # summarization rule capabilities: groq 0.6, gemini 1.0.
    scored = rank_models(
        "summarization", ["groq", "gemini"], ML_OFF_STATS,
        CAPABILITY_ONLY_WEIGHTS,  # isolate the capability component fully
        prompt="summarize this",
        ml_classifier=StubMLClassifier({"gemini": 0.0, "groq": 1.0}),
    )
    by_name = {r.provider_name: r.score for r in scored}

    # groq blended capability: 0.6*1.0 + 0.4*0.6 = 0.84
    assert by_name["groq"] == pytest.approx(0.84)
    # gemini blended capability: 0.6*0.0 + 0.4*1.0 = 0.4
    assert by_name["gemini"] == pytest.approx(0.4)
    # The ML prediction genuinely flipped the ranking (gemini was #1 rule-based).
    assert scored[0].provider_name == "groq"


def test_unseen_provider_degrades_to_rule_based_capability(monkeypatch):
    """A provider missing from the ML prediction keeps its rule-based
    capability (no arbitrary zero)."""
    monkeypatch.setattr("app.config.settings.ENABLE_ML_ROUTING", True)
    pure = rank_models("summarization", ["groq", "gemini", "deepseek"], ML_OFF_STATS,
                       CAPABILITY_ONLY_WEIGHTS)
    blended = rank_models(
        "summarization", ["groq", "gemini", "deepseek"], ML_OFF_STATS,
        CAPABILITY_ONLY_WEIGHTS,
        prompt="summarize this",
        ml_classifier=StubMLClassifier({"gemini": 1.0, "groq": 0.0}),  # no deepseek
    )
    # deepseek: rule capability 0.5 → same score in both runs.
    deepseek_pure = next(r for r in pure if r.provider_name == "deepseek")
    deepseek_blend = next(r for r in blended if r.provider_name == "deepseek")
    assert deepseek_blend.score == deepseek_pure.score == pytest.approx(0.5)


def test_low_confidence_ml_prediction_falls_back_to_rules(monkeypatch):
    """predict_best_provider()==None (model's own confidence gate) → pure
    rule-based scores even with the flag on."""
    monkeypatch.setattr("app.config.settings.ENABLE_ML_ROUTING", True)
    pure = rank_models("summarization", ["groq", "gemini"], ML_OFF_STATS,
                       CAPABILITY_ONLY_WEIGHTS)
    blended = rank_models(
        "summarization", ["groq", "gemini"], ML_OFF_STATS,
        CAPABILITY_ONLY_WEIGHTS,
        prompt="summarize this",
        ml_classifier=StubMLClassifier(None),  # low-confidence → None
    )
    assert [r.score for r in blended] == [r.score for r in pure]


def test_unloaded_model_is_ignored_even_with_flag(monkeypatch):
    monkeypatch.setattr("app.config.settings.ENABLE_ML_ROUTING", True)
    pure = rank_models("summarization", ["groq", "gemini"], ML_OFF_STATS)
    with_unloaded = rank_models(
        "summarization", ["groq", "gemini"], ML_OFF_STATS,
        prompt="summarize this",
        ml_classifier=StubMLClassifier({"gemini": 0.9, "groq": 0.1}, loaded=False),
    )
    assert [r.score for r in with_unloaded] == [r.score for r in pure]


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
