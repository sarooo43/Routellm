"""Judge-based response quality scoring (Part A of the ML routing phase).

An LLM-as-judge that rates a (prompt, response) pair on a 0.0–1.0 scale.
Used ONLY by the offline data-collection script (scripts/
collect_training_data.py) — never on live requests, since it doubles API
usage per request.

Parsing is deliberately defensive: a judge that hallucinates non-JSON,
gives an out-of-range score, or fails entirely yields a neutral 0.5
instead of crashing data collection. A closed/failed judge call also
degrades to 0.5 — collect continues and the pair simply contributes a
neutral label rather than an exception.
"""

import json
import re
from typing import Optional

from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)

# Preference order for the judge itself: pick the strongest configured
# generalist, independent of which providers are being scored.
JUDGE_PROVIDER_PREFERENCE: tuple[str, ...] = ("deepseek", "gemini", "groq")

NEUTRAL_SCORE = 0.5

JUDGE_PROMPT_TEMPLATE = """You are an impartial quality judge for LLM responses.

Rate the RESPONSE below on how well it answers the PROMPT. Judge it on
relevance (does it address the request), correctness (is it accurate and
sensible), and completeness (nothing important missing).

Score strictly between 0.0 (worthless) and 1.0 (excellent). A terse but
correct answer can score high; a pretty but wrong answer must not.

PROMPT:
{prompt}

RESPONSE:
{response}

Reply with ONLY a JSON object, no other text, in this exact shape:
{{"score": 0.85, "reasoning": "<one short sentence>"}}"""

_SCORE_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


def _pick_judge_provider():
    """First configured judge-capable provider, or None."""
    for name in JUDGE_PROVIDER_PREFERENCE:
        provider = PROVIDER_REGISTRY.get(name)
        if provider is not None:
            return provider
    # Last resort: any registered provider at all.
    for provider in PROVIDER_REGISTRY.values():
        return provider
    return None


def parse_judge_score(raw_text: str) -> float:
    """Defensively extract a 0.0–1.0 score from the judge's raw output.

    Tries strict JSON first, then generic number salvage. Anything
    unparseable or out of range → neutral 0.5.
    """
    if not raw_text or not raw_text.strip():
        return NEUTRAL_SCORE

    candidate: Optional[float] = None
    try:
        # Some judges wrap JSON in code fences or chatter; grab the first
        # {...} block before parsing.
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if match:
            payload = json.loads(match.group(0))
            value = payload.get("score")
            if isinstance(value, (int, float)):
                candidate = float(value)
            elif isinstance(value, str):
                num = _SCORE_PATTERN.search(value)
                if num:
                    candidate = float(num.group(0))
    except (json.JSONDecodeError, AttributeError, TypeError):
        candidate = None

    if candidate is None:
        # Salvage: first bare number in the text (e.g. "Score: 0.8").
        num = _SCORE_PATTERN.search(raw_text)
        if num:
            try:
                candidate = float(num.group(0))
            except ValueError:
                candidate = None

    if candidate is None:
        logger.warning("judge_parse_failed_using_neutral", raw=raw_text[:200])
        return NEUTRAL_SCORE

    if candidate > 1.0 and candidate <= 100.0:
        candidate = candidate / 100.0  # judge used a 0–100 scale
    if candidate < 0.0 or candidate > 1.0:
        logger.warning("judge_score_out_of_range_using_neutral", score=candidate)
        return NEUTRAL_SCORE
    return round(candidate, 4)


async def judge_response_quality(prompt: str, response_text: str) -> float:
    """Rate response_text against prompt on a 0.0–1.0 quality scale.

    One judge call per (prompt, response) pair. Failures degrade to the
    neutral 0.5 — data collection must not crash on a single bad judge
    response.
    """
    judge = _pick_judge_provider()
    if judge is None:
        logger.warning("judge_no_provider_configured_using_neutral")
        return NEUTRAL_SCORE

    # Structured rubric + JSON-only output; temperature 0 for consistency.
    judge_prompt = JUDGE_PROMPT_TEMPLATE.format(
        prompt=(prompt or "")[:6000],
        response=(response_text or "")[:6000],
    )
    result = await judge.generate(judge_prompt, temperature=0.0)

    if not result.success:
        logger.warning(
            "judge_call_failed_using_neutral",
            provider=judge.provider_name,
            error=result.error,
        )
        return NEUTRAL_SCORE

    return parse_judge_score(result.text)
