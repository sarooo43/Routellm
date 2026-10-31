"""Model scoring and ranking — the routing decision engine.

Pure functions: all inputs are passed in, nothing is fetched from DB/Redis,
no side effects. This keeps scoring trivially testable and decoupled from
data access.
"""

from typing import Optional, TYPE_CHECKING

from pydantic import BaseModel

if TYPE_CHECKING:
    # Typing-only: the real import is deferred so environments without the
    # optional ML deps can import scorer freely.
    from app.core.ml_classifier import MLRouterClassifier

# How well each provider is expected to handle each task type (0.0-1.0).
# 0.3 fallback applies to any unmapped provider/task combo so it still gets
# some chance rather than zero.
CAPABILITY_MATCH = {
    "code": {"deepseek": 1.0, "groq": 0.5, "gemini": 0.4},
    "reasoning": {"deepseek": 0.9, "gemini": 0.6, "groq": 0.5},
    "summarization": {"gemini": 1.0, "groq": 0.6, "deepseek": 0.5},
    "qa": {"groq": 1.0, "gemini": 0.8, "deepseek": 0.6},
    "general": {"groq": 0.9, "gemini": 0.9, "deepseek": 0.7},
}

# Rough cost estimates ($ per 1K tokens) — refined with real data later.
COST_PER_1K_TOKENS = {"groq": 0.0, "gemini": 0.0, "deepseek": 0.0002}

# Rough expected latency (ms) — used when no historical stats exist yet.
BASELINE_LATENCY_MS = {"groq": 300, "gemini": 600, "deepseek": 900}

SLOWEST_BASELINE_MS = max(BASELINE_LATENCY_MS.values())

# Default scoring weights (must stay explainable: they sum to 1.0).
DEFAULT_WEIGHTS = {
    "capability": 0.35,
    "success_rate": 0.30,
    "cost": 0.15,
    "latency": 0.10,
    "availability": 0.10,
}

AVAILABILITY_SCORES = {
    "CLOSED": 1.0,
    "HALF_OPEN": 0.5,
    "OPEN": 0.0,
}

UNMATCHED_CAPABILITY_DEFAULT = 0.3

# EXPERIMENTAL ML routing: how much a learned capability prediction counts
# over the hand-tuned CAPABILITY_MATCH value when blended (60% ML / 40%
# rules). The ML model is trained on limited judge-labeled data and should
# not blindly dominate the proven heuristic.
ML_CAPABILITY_WEIGHT = 0.6


class ModelStats(BaseModel):
    provider_name: str
    task_type: str
    total_requests: int = 0
    successful_requests: int = 0
    avg_latency_ms: float = 0.0
    circuit_state: str = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
    # Human quality signal (thumbs up/down), ALWAYS sparse: most requests
    # never get rated, while API success/failure is tracked on every call.
    positive_feedback_count: int = 0
    negative_feedback_count: int = 0

    @property
    def success_rate(self) -> float:
        if self.total_requests == 0:
            return 0.75  # optimistic default for brand-new models with no history
        return self.successful_requests / self.total_requests

    @property
    def feedback_adjusted_success_rate(self) -> float:
        """Blends raw API success_rate with human feedback.

        The two signals have wildly different sample sizes (every request
        has an API outcome; only a small fraction ever gets a thumbs
        rating), so feedback is blended using a confidence weight that
        grows with feedback volume:

        - 0 feedback → pure API success_rate, untouched.
        - The weight rises linearly with the number of ratings but is
          CAPPED at 0.5, so feedback can never fully override the much
          larger sample of raw API outcomes — even with hundreds of
          ratings, the effective rate stays at most 50% feedback-informed.
        """
        total_feedback = self.positive_feedback_count + self.negative_feedback_count
        if total_feedback == 0:
            return self.success_rate  # no feedback yet, pure API-based rate

        feedback_rate = self.positive_feedback_count / total_feedback

        # Confidence weight grows with feedback volume, capped at 0.5 —
        # feedback can shift the score by at most 50%, never fully override
        # the much larger sample of raw API outcomes.
        feedback_weight = min(0.5, total_feedback / 20)

        return (
            (1 - feedback_weight) * self.success_rate
            + feedback_weight * feedback_rate
        )


class RankedModel(BaseModel):
    provider_name: str
    score: float
    confidence_score: float
    circuit_state: str


def normalize_cost(provider_name: str) -> float:
    """0 = free/cheapest, 1 = most expensive relative to the priciest provider.

    Unknown providers are treated as free (optimistic). Result is clamped
    to the 0-1 range.
    """
    max_cost = max(COST_PER_1K_TOKENS.values())
    if max_cost <= 0:
        return 0.0
    cost = COST_PER_1K_TOKENS.get(provider_name, 0.0)
    return min(1.0, cost / max_cost)


def normalize_latency(
    provider_name: str, actual_avg_latency: Optional[float] = None
) -> float:
    """0 = fastest, 1 = slowest, relative to the slowest baseline (900ms).

    Uses historical avg latency when provided, otherwise the provider's
    baseline. Unknown providers fall back to the slowest baseline
    (conservative). Actual latencies are clamped at 1.0.
    """
    if actual_avg_latency is None:
        actual = float(BASELINE_LATENCY_MS.get(provider_name, SLOWEST_BASELINE_MS))
    else:
        actual = float(actual_avg_latency)
    if SLOWEST_BASELINE_MS <= 0:
        return 0.0
    return min(1.0, max(0.0, actual) / SLOWEST_BASELINE_MS)


def _score_provider(
    provider_name: str,
    task_type: str,
    stats: dict[str, ModelStats],
    weights: dict,
    rule_capability: float,
    ml_capability: Optional[float] = None,
) -> RankedModel:
    provider_stats = stats.get(provider_name) or ModelStats(
        provider_name=provider_name, task_type=task_type
    )

    # Hybrid capability: when an ML prediction supplies a learned value for
    # this provider it is blended 60/40 over the rule-based score — the ML
    # model is trained on limited data and must not blindly override the
    # proven heuristic table. No ML value (flag off, model missing, low
    # confidence, provider unseen) → pure rule-based, exactly as v1.
    if ml_capability is not None:
        capability_match = ML_CAPABILITY_WEIGHT * ml_capability + (
            1 - ML_CAPABILITY_WEIGHT
        ) * rule_capability
    else:
        capability_match = rule_capability

    # Feedback-adjusted, NOT raw: a provider getting consistent thumbs-down
    # on a task type ranks lower for that task type even if its raw API
    # success rate (no errors) looks fine.
    success_rate = provider_stats.feedback_adjusted_success_rate
    cost_score = 1.0 - normalize_cost(provider_name)
    latency_score = 1.0 - normalize_latency(
        provider_name,
        provider_stats.avg_latency_ms if provider_stats.total_requests > 0 else None,
    )
    availability = AVAILABILITY_SCORES.get(provider_stats.circuit_state, 1.0)

    score = (
        weights["capability"] * capability_match
        + weights["success_rate"] * success_rate
        + weights["cost"] * cost_score
        + weights["latency"] * latency_score
        + weights["availability"] * availability
    )

    return RankedModel(
        provider_name=provider_name,
        score=score,
        confidence_score=round(success_rate * capability_match, 3),
        circuit_state=provider_stats.circuit_state,
    )


def rank_models(
    task_type: str,
    available_providers: list[str],
    stats: dict[str, ModelStats],
    weights: Optional[dict] = None,
    prompt: Optional[str] = None,
    ml_classifier: Optional["MLRouterClassifier"] = None,
) -> list[RankedModel]:
    """Score and rank all available providers for a given task type.

    ``stats`` is keyed by provider_name (caller fetches the right slice —
    no DB access here). Unknown task types get the low default capability
    of 0.3 for every provider; missing stats entries use fresh defaults
    (optimistic 0.75 success rate). Custom weights are merged over the
    defaults. Providers with an OPEN circuit ALWAYS rank last regardless
    of their raw score.

    EXPERIMENTAL ML routing: when ``prompt`` + ``ml_classifier`` are both
    provided AND settings.ENABLE_ML_ROUTING is on AND the classifier loaded
    a trained model, capability scores come from a 60/40 blend of the ML
    prediction with the rule-based table (see ML_CAPABILITY_WEIGHT). Under
    any other condition the behavior is bit-for-bit identical to pure
    rule-based scoring.
    """
    effective_weights = {**DEFAULT_WEIGHTS, **(weights or {})}

    ml_capability_by_provider = _ml_capability_scores(
        prompt, ml_classifier, task_type, available_providers
    )

    ranked = [
        _score_provider(
            name,
            task_type,
            stats,
            effective_weights,
            rule_capability=CAPABILITY_MATCH.get(task_type, {}).get(
                name, UNMATCHED_CAPABILITY_DEFAULT
            ),
            ml_capability=ml_capability_by_provider.get(name) if ml_capability_by_provider else None,
        )
        for name in available_providers
    ]

    # Push OPEN circuits to the bottom no matter how well they scored;
    # keep everything else (and the OPEN group itself) ordered by score DESC.
    ranked.sort(key=lambda r: (0 if r.circuit_state != "OPEN" else 1, -r.score))
    return ranked


def _ml_capability_scores(
    prompt: Optional[str],
    ml_classifier: Optional["MLRouterClassifier"],
    task_type: str,
    available_providers: list[str],
) -> Optional[dict[str, float]]:
    """Optional learned capability scores (EXPERIMENTAL).

    Enabled only when ALL of: a prompt is given, a classifier is supplied,
    ENABLE_ML_ROUTING is on, and the model is loaded + confident. Ensemble
    providers missing from the prediction degrade to the rule-based value
    instead of an arbitrary zero.
    """
    if ml_classifier is None or prompt is None:
        return None

    from app.config import settings  # deferred: keeps scorer import light

    if not settings.ENABLE_ML_ROUTING:
        return None

    probs = ml_classifier.predict_best_provider(prompt)
    if probs is None:
        # Not loaded or below the confidence floor → pure rule-based.
        return None

    rule_for_missing = lambda name: CAPABILITY_MATCH.get(task_type, {}).get(  # noqa: E731
        name, UNMATCHED_CAPABILITY_DEFAULT
    )
    return {
        name: probs.get(name, rule_for_missing(name))
        for name in available_providers
    }
