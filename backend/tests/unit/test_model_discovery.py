"""Model discovery, caching, refresh-on-404 and the env override.

The behaviour these protect: the app must never assume a model exists. It asks
the provider which models this key can use, prefers a stable order, and
re-asks when a request comes back model_not_found.
"""

import httpx
import pytest

from app.providers.model_registry import (
    MODEL_CACHE_TTL_S,
    ModelCatalog,
    PROVIDER_SPECS,
    choose_model,
    is_chat_capable,
)

GROQ_MODELS = {
    "data": [
        {"id": "whisper-large-v3"},          # audio → excluded
        {"id": "distil-whisper-large-v3-en"},
        {"id": "nomic-embed-text-v1_5"},      # embedding → excluded
        {"id": "llama-3.1-8b-instant"},       # chat → preferred (2nd in order)
        {"id": "some-brand-new-model"},      # chat but not preferred
    ]
}

GEMINI_MODELS = {
    "models": [
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        {"name": "models/gemini-2.0-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
    ]
}


# --------------------------------------------------------------------------
# Preference order
# --------------------------------------------------------------------------


def test_preference_list_wins_over_provider_order():
    """Provider order is arbitrary; our ordered preference is deliberate."""
    spec = PROVIDER_SPECS["groq"]
    available = ["some-brand-new-model", "llama-3.1-8b-instant"]
    assert choose_model(spec, available) == "llama-3.1-8b-instant"


def test_first_available_preference_is_used():
    spec = PROVIDER_SPECS["groq"]
    assert choose_model(spec, ["llama-3.1-8b-instant", "openai/gpt-oss-120b"]) == "llama-3.1-8b-instant"
    assert choose_model(spec, ["openai/gpt-oss-120b", "gemma2-9b-it"]) == "openai/gpt-oss-120b"


def test_unknown_models_fall_back_to_the_first_chat_model():
    spec = PROVIDER_SPECS["groq"]
    assert choose_model(spec, ["totally-new-chat-model"]) == "totally-new-chat-model"


def test_empty_list_falls_back_to_the_static_default():
    spec = PROVIDER_SPECS["groq"]
    assert choose_model(spec, []) == spec.default_model


@pytest.mark.parametrize("model_id,expected", [
    ("llama-3.1-8b-instant", True),
    ("whisper-large-v3", False),
    ("nomic-embed-text", False),
    ("text-embedding-004", False),
])
def test_chat_capability_filter(model_id, expected):
    assert is_chat_capable(PROVIDER_SPECS["groq"], model_id) is expected


# --------------------------------------------------------------------------
# Fetching both list shapes
# --------------------------------------------------------------------------


async def test_openai_shaped_list_is_filtered_and_ordered(respx_mock):
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    catalog = ModelCatalog()
    models, source = await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890")

    assert source == "discovered"
    assert "whisper-large-v3" not in models
    assert "nomic-embed-text-v1_5" not in models
    assert "llama-3.1-8b-instant" in models


async def test_gemini_shaped_list_strips_the_models_prefix(respx_mock):
    respx_mock.get("https://generativelanguage.googleapis.com/v1beta/models").mock(
        return_value=httpx.Response(200, json=GEMINI_MODELS)
    )
    catalog = ModelCatalog()
    models, _ = await catalog.list_models(PROVIDER_SPECS["gemini"], "AIzaTestKey123456")

    assert "gemini-2.0-flash" in models
    assert all(not m.startswith("models/") for m in models)
    assert "text-embedding-004" not in models


async def test_discovery_failure_degrades_instead_of_raising(respx_mock):
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    catalog = ModelCatalog()
    models, source = await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890")
    assert models == []
    assert source == "unavailable"


async def test_discovery_network_error_degrades(respx_mock):
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        side_effect=httpx.ConnectError("blocked")
    )
    catalog = ModelCatalog()
    assert await catalog.list_models(PROVIDER_SPECS["groq"], "k" * 12) == ([], "unavailable")


# --------------------------------------------------------------------------
# Caching
# --------------------------------------------------------------------------


async def test_second_call_hits_the_cache(respx_mock):
    route = respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    catalog = ModelCatalog()
    await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890")
    await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890")

    assert route.call_count == 1


async def test_cache_expires_after_the_ttl(respx_mock):
    route = respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    catalog = ModelCatalog(ttl_s=0.01)
    await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890")
    import asyncio
    await asyncio.sleep(0.02)
    await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890")
    assert route.call_count == 2


async def test_force_refresh_bypasses_the_cache(respx_mock):
    route = respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    catalog = ModelCatalog()
    await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890")
    await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890", force_refresh=True)
    assert route.call_count == 2


async def test_different_keys_do_not_share_a_cache_entry(respx_mock):
    """Two accounts can see different models; the cache is per credential."""
    route = respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    catalog = ModelCatalog()
    await catalog.list_models(PROVIDER_SPECS["groq"], "key-one-1234567890")
    await catalog.list_models(PROVIDER_SPECS["groq"], "key-two-1234567890")
    assert route.call_count == 2


async def test_invalidate_forces_rediscovery(respx_mock):
    route = respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    catalog = ModelCatalog()
    await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890")
    await catalog.invalidate_everywhere("groq")  # every key, both tiers
    await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_test_1234567890")
    assert route.call_count == 2


def test_cache_does_not_store_the_raw_key():
    """A 1-hour cache must not keep plaintext credentials in its keys."""
    catalog = ModelCatalog()
    catalog.store("groq", "gsk_VERYSECRET1234567890", ["m"])
    assert all("gsk_VERYSECRET1234567890" not in str(k) for k in catalog._cache)


def test_ttl_default_is_about_an_hour():
    assert MODEL_CACHE_TTL_S == 3600.0


def test_the_ttl_is_configurable_and_clamped(monkeypatch):
    """The documented MODEL_CACHE_TTL_SECONDS knob must actually do something."""
    monkeypatch.setattr("app.config.settings.MODEL_CACHE_TTL_SECONDS", 60, raising=False)
    assert ModelCatalog()._ttl == 60.0
    monkeypatch.setattr("app.config.settings.MODEL_CACHE_TTL_SECONDS", -5, raising=False)
    assert ModelCatalog()._ttl == 0.0
    monkeypatch.setattr("app.config.settings.MODEL_CACHE_TTL_SECONDS", "nonsense", raising=False)
    assert ModelCatalog()._ttl == MODEL_CACHE_TTL_S


# --------------------------------------------------------------------------
# Refresh on model_not_found
# --------------------------------------------------------------------------


async def test_model_not_found_invalidates_the_cache():
    """A 404 means our cached model list is stale — the next call must re-ask."""
    from app.core import executor

    catalog = ModelCatalog()
    catalog.store("groq", None, ["stale-model"])
    assert catalog.peek("groq", None) == ["stale-model"]

    import app.providers.model_registry as registry
    registry.model_catalog.store("groq", None, ["stale-model"])

    await executor._invalidate_models("groq", None)
    assert registry.model_catalog.peek("groq", None) is None


# --------------------------------------------------------------------------
# Env override
# --------------------------------------------------------------------------


async def test_env_override_wins_over_discovery(monkeypatch, respx_mock):
    from app.providers.model_registry import resolve_model

    monkeypatch.setattr("app.config.settings.GROQ_MODEL", "my-pinned-model", raising=False)
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    import app.providers.model_registry as registry
    registry.model_catalog.clear()

    model, _available, source = await resolve_model("groq", "gsk_test_1234567890", override="my-pinned-model")
    assert model == "my-pinned-model"
    assert source == "override"


async def test_without_override_the_discovered_model_is_used(monkeypatch, respx_mock):
    from app.providers.model_registry import resolve_model

    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    import app.providers.model_registry as registry
    registry.model_catalog.clear()

    model, available, source = await resolve_model("groq", "gsk_test_1234567890")
    assert model == "llama-3.1-8b-instant"  # first available preference
    assert source == "discovered"
    assert available


async def test_unsupported_provider_is_not_invented():
    from app.providers.model_registry import resolve_model

    model, models, source = await resolve_model("signs-of-the-zodiac", None)
    assert model == ""
    assert models == []
    assert source == "unsupported"
