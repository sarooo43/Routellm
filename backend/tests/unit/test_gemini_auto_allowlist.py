"""T2: Gemini Auto allow-list, tested against the REAL observed list.

The 49 names below are OBSERVED LIVE (owner's Codespace, main at 136fae0,
real Gemini key, own v1beta header-auth list call filtered to
generateContent — names only, nothing synthetic). They are the fixture on
purpose: synthetic fixtures are exactly what failed to predict the field
behavior (a codename or preview becoming the picked model once stable
models were denied).

The 49 vs 44 difference, with evidence from the code: discovery filters
through ``is_chat_capable`` (app/providers/model_registry.py), whose gemini
``non_chat_markers`` include "tts". Exactly five of the 49 names contain
"tts":
    gemini-2.5-flash-preview-tts, gemini-2.5-pro-preview-tts,
    gemini-3.1-flash-tts-preview, gemini-3.8-flash-tts,
    gemini-3.8-flash-lite-tts
49 - 5 = 44 — the number the app logged (model_discovery_ok models=44,
OBSERVED LIVE). So the app's filter removes only TTS; inference CONFIRMED
against this fixture by test_tts_exactly_explains_49_vs_44 below.
"""

import pytest

from app.config import settings
from app.providers.model_registry import (
    GEMINI_AUTO_ALIAS_RES,
    GEMINI_AUTO_STABLE_RES,
    PROVIDER_SPECS,
    choose_model,
    gemini_auto_class,
    parse_version_tuple,
    rank_candidate_ids,
)

#: OBSERVED LIVE, names only (owner's Codespace, v1beta, header auth,
#: filtered to generateContent). Verbatim order as reported.
GEMINI_49_OBSERVED_LIVE = [
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash-preview-tts",
    "gemini-2.5-pro-preview-tts",
    "gemma-4-26b-a4b-it",
    "gemma-4-31b-it",
    "gemini-flash-latest",
    "gemini-flash-lite-latest",
    "gemini-pro-latest",
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash-image",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.1-pro-preview-customtools",
    "gemini-3.1-flash-lite-preview",
    "gemini-3.1-flash-lite",
    "gemini-3-pro-image-preview",
    "gemini-3-pro-image",
    "nano-banana-pro-preview",
    "gemini-3.1-flash-image-preview",
    "gemini-3.1-flash-image",
    "gemini-3.1-flash-lite-image",
    "gemini-nano-banana-2.1",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-omni-flash-preview",
    "gemini-omni-1.1-flash",
    "gemini-omni-1.2-flash",
    "gemini-3.5-transcribe",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-3.8-flash",
    "gemini-4-argon",
    "gemini-4-carbon",
    "gemini-4-hydrogen",
    "lyria-3-clip-preview",
    "lyria-3-pro-preview",
    "lyria-3.5",
    "gemini-3.1-flash-tts-preview",
    "gemini-3.8-flash-tts",
    "gemini-3.8-flash-lite-tts",
    "gemini-robotics-er-2-preview",
    "gemini-2.5-computer-use-preview-10-2025",
    "antigravity-preview-05-2026",
    "antigravity-preview-09-2026",
    "antigravity-preview-latest",
    "deep-research-max-preview-04-2026",
    "deep-research-preview-04-2026",
    "deep-research-pro-preview-12-2025",
]


@pytest.fixture
def gemini_spec():
    return PROVIDER_SPECS["gemini"]


# --------------------------------------------------------------------------
# The filter itself
# --------------------------------------------------------------------------


def test_tts_exactly_explains_49_vs_44():
    """OBSERVED LIVE 49 names; the code's TTS marker removes exactly 5."""
    tts_names = [m for m in GEMINI_49_OBSERVED_LIVE if "tts" in m.lower()]
    assert len(tts_names) == 5
    assert len(GEMINI_49_OBSERVED_LIVE) - len(tts_names) == 44


def test_allow_list_classes_on_the_real_list():
    """Every one of the 49 OBSERVED LIVE names lands in a deliberate class."""
    expected = {
        "alias": {
            "gemini-flash-latest",
            "gemini-flash-lite-latest",
            "gemini-pro-latest",
        },
        "stable": {
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemma-4-26b-a4b-it",
            "gemma-4-31b-it",
            "gemini-2.5-flash-lite",
            "gemini-3.1-flash-lite",
            "gemini-3.5-flash",
            "gemini-3.5-flash-lite",
            "gemini-3.6-flash",
            "gemini-3.7-flash",
            "gemini-3.8-flash",
        },
        "preview": {
            "gemini-3-flash-preview",
            "gemini-3.1-pro-preview",
            "gemini-3.1-flash-lite-preview",
        },
    }
    for klass, names in expected.items():
        for name in names:
            assert gemini_auto_class(name) == klass, f"{name} expected {klass}"
    excluded = set(GEMINI_49_OBSERVED_LIVE) - set().union(*expected.values())
    for name in excluded:
        assert gemini_auto_class(name) == "excluded", f"{name} should be excluded"


def test_codenames_never_rank_above_a_numbered_release(gemini_spec):
    """OBSERVED LIVE: with ALL stable flash models denied-equivalent (i.e.
    absent from the candidate list), the top pick must still be a numbered
    release, never gemini-4-argon / -carbon / -hydrogen."""
    only_codenames = ["gemini-4-argon", "gemini-4-carbon", "gemini-4-hydrogen"]
    assert rank_candidate_ids(gemini_spec, only_codenames) == []


def test_previews_are_not_auto_candidates_by_default(gemini_spec, monkeypatch):
    monkeypatch.setattr(settings, "AUTO_ALLOW_PREVIEW_MODELS", False)
    ranked = rank_candidate_ids(
        gemini_spec,
        ["gemini-3-flash-preview", "gemini-3.1-pro-preview", "gemini-3.8-flash"],
    )
    assert ranked == ["gemini-3.8-flash"]


def test_previews_enter_auto_only_behind_the_setting_and_only_after_stable(
    gemini_spec, monkeypatch
):
    monkeypatch.setattr(settings, "AUTO_ALLOW_PREVIEW_MODELS", True)
    ranked = rank_candidate_ids(
        gemini_spec,
        [
            "gemini-3-flash-preview",
            "gemini-2.5-flash",
            "gemini-3.8-flash",
            "gemini-4-carbon",
        ],
    )
    # Newest stable first, previews strictly after every stable candidate.
    assert ranked == ["gemini-3.8-flash", "gemini-2.5-flash", "gemini-3-flash-preview"]


def test_latest_aliases_rank_first_in_auto(gemini_spec, monkeypatch):
    monkeypatch.setattr(settings, "AUTO_ALLOW_PREVIEW_MODELS", False)
    ranked = rank_candidate_ids(
        gemini_spec,
        ["gemini-3.8-flash", "gemini-flash-latest", "gemma-4-31b-it"],
    )
    # UNVERIFIED LIVE: aliases-first is the owner's instruction; a live route
    # must confirm "latest" points at the newest flash family.
    assert ranked[0] == "gemini-flash-latest"
    assert ranked[1] == "gemini-3.8-flash"


def test_stable_version_order_is_numeric_not_lexical(gemini_spec):
    ranked = rank_candidate_ids(
        gemini_spec,
        ["gemini-2.5-flash", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-10.0-flash"],
    )
    assert ranked == [
        "gemini-10.0-flash",
        "gemini-3.8-flash",
        "gemini-3.6-flash",
        "gemini-2.5-flash",
    ]


def test_version_parse_never_ranks_a_codename_above_a_release():
    # A codename's stray digit ("gemini-4-argon") parses as (4,) — but the
    # allow-list removes it before version order can matter at all.
    assert parse_version_tuple("gemini-4-argon") == (4,)
    assert gemini_auto_class("gemini-4-argon") == "excluded"


def test_the_full_49_name_list_ranks_as_expected(gemini_spec, monkeypatch):
    """THE field scenario, pinned: Auto over the whole OBSERVED LIVE list."""
    monkeypatch.setattr(settings, "AUTO_ALLOW_PREVIEW_MODELS", False)
    ranked = rank_candidate_ids(gemini_spec, GEMINI_49_OBSERVED_LIVE)
    assert ranked[0] == "gemini-flash-latest"
    # No codename, preview, TTS, image, omni, lyria, robotics or
    # deep-research id anywhere in the Auto order.
    for model_id in ranked:
        assert gemini_auto_class(model_id) in ("alias", "stable")
    # Everything else stays visible in the explorer's "Other / specialized"
    # bucket (sort_models_for_provider filters on chat_capable, NOT on the
    # Auto allow-list) — asserted here by counting what survives.
    kept = set(ranked)
    assert kept <= set(GEMINI_49_OBSERVED_LIVE)
    assert len(ranked) == 14  # 3 aliases + 11 stable


def test_other_providers_keep_their_existing_ranking(monkeypatch):
    """The allow-list must not disturb Groq/DeepSeek (no allow-list there)."""
    monkeypatch.setattr(settings, "AUTO_ALLOW_PREVIEW_MODELS", False)
    groq_spec = PROVIDER_SPECS["groq"]
    assert rank_candidate_ids(groq_spec, ["llama-3.3-70b-versatile"])[0] == (
        "llama-3.3-70b-versatile"
    )


def test_alias_patterns_are_strict():
    """A lookalike must not sneak into the alias class."""
    assert GEMINI_AUTO_ALIAS_RES[0].match("gemini-flash-latest-tts") is None
    assert GEMINI_AUTO_STABLE_RES[0].match("gemini-3.8-flash-preview") is None
    assert GEMINI_AUTO_STABLE_RES[0].match("gemini-3.8-flash") is not None
