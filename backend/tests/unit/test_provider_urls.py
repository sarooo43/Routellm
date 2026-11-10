"""The exact provider URLs, asserted verbatim.

These look pedantic on purpose. A wrong URL is invisible until a key is added
and a prompt is sent, and when it failed it surfaced as "your key is invalid".
Asserting the literal string is the only way to keep that from drifting again —
this suite already caught a real ``.../models/models/<model>`` on the first run
of the new builder.
"""

import pytest

from app.providers.model_registry import PROVIDER_SPECS, spec_for

GROQ_CHAT = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_CHAT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
DEEPSEEK_CHAT = "https://api.deepseek.com/v1/chat/completions"


# --------------------------------------------------------------------------
# Chat URLs
# --------------------------------------------------------------------------


def test_groq_chat_url_is_openai_compatible_under_openai_v1():
    """Groq's OpenAI-compatible surface lives under /openai/v1 — not /v1."""
    assert PROVIDER_SPECS["groq"].chat_url("llama-3.1-8b-instant") == GROQ_CHAT


def test_gemini_chat_url_puts_the_model_in_the_path():
    assert (
        PROVIDER_SPECS["gemini"].chat_url("gemini-2.0-flash")
        == GEMINI_CHAT.format(model="gemini-2.0-flash")
    )


def test_gemini_never_doubles_the_models_prefix():
    """A discovered id may already be "models/x"; the prefix must not repeat."""
    url = PROVIDER_SPECS["gemini"].chat_url("models/gemini-2.0-flash")
    assert url.count("models/") == 1
    assert "/models/models/" not in url


def test_deepseek_chat_url():
    assert PROVIDER_SPECS["deepseek"].chat_url("deepseek-chat") == DEEPSEEK_CHAT


@pytest.mark.parametrize("provider", ["groq", "gemini", "deepseek"])
def test_no_built_url_has_a_double_slash_after_the_scheme(provider):
    spec = PROVIDER_SPECS[provider]
    for url in (spec.chat_url("some-model"), spec.models_url()):
        assert "://" in url
        assert "//" not in url.split("://", 1)[1], url


@pytest.mark.parametrize("provider", ["groq", "deepseek"])
def test_openai_style_base_ends_with_v1_once(provider):
    url = PROVIDER_SPECS[provider].chat_url("m")
    assert url.count("/v1/") == 1, url
    assert "/v1/v1/" not in url


def test_gemini_has_the_v1beta_prefix_and_models_segment():
    url = PROVIDER_SPECS["gemini"].chat_url("gemini-2.0-flash")
    assert "/v1beta/models/" in url


# --------------------------------------------------------------------------
# Model-list URLs (the ones discovery calls)
# --------------------------------------------------------------------------


def test_model_list_urls():
    assert PROVIDER_SPECS["groq"].models_url() == "https://api.groq.com/openai/v1/models"
    assert (
        PROVIDER_SPECS["gemini"].models_url()
        == "https://generativelanguage.googleapis.com/v1beta/models"
    )
    assert PROVIDER_SPECS["deepseek"].models_url() == "https://api.deepseek.com/v1/models"


@pytest.mark.parametrize("provider", ["groq", "gemini", "deepseek"])
def test_spec_host_is_the_api_host(provider):
    spec = PROVIDER_SPECS[provider]
    assert spec.host in spec.chat_url("m")
    assert spec.host.startswith("api.") or spec.host.startswith("generativelanguage.")


def test_unknown_provider_had_no_spec():
    assert spec_for("nope") is None
