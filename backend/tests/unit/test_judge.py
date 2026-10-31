"""Defensive parsing tests for the LLM-as-judge (judge.py).

The mock-first design means these run WITHOUT any API key or network —
parse_judge_score is a pure function.
"""

from app.core.judge import NEUTRAL_SCORE, parse_judge_score  # noqa


def test_strict_json_parsed():
    assert parse_judge_score('{"score": 0.85, "reasoning": "good"}') == 0.85


def test_json_wrapped_in_chatter_or_fences():
    raw = 'Sure! ```json\n{"score": 0.3, "reasoning": "weak"}\n``` hope that helps'
    assert parse_judge_score(raw) == 0.3


def test_score_as_string_value():
    assert parse_judge_score('{"score": "0.7", "reasoning": "ok"}') == 0.7


def test_zero_to_hundred_scale_normalized():
    assert parse_judge_score('{"score": 85, "reasoning": "good"}') == 0.85


def test_bare_number_salvaged_when_not_json():
    assert parse_judge_score("Score: 0.65") == 0.65


def test_unparseable_returns_neutral_not_crash():
    assert parse_judge_score("I refuses to comply 🤪") == NEUTRAL_SCORE


def test_empty_response_returns_neutral():
    assert parse_judge_score("") == NEUTRAL_SCORE
    assert parse_judge_score(None) == NEUTRAL_SCORE


def test_out_of_range_scores_clamped_to_neutral():
    # Negative scores can't mean anything sensible → neutral.
    assert parse_judge_score('{"score": -5.0, "reasoning": "x"}') == NEUTRAL_SCORE
    # Scores beyond a 0-100 scale are nonsense → neutral.
    assert parse_judge_score('{"score": 250.0, "reasoning": "x"}') == NEUTRAL_SCORE
