"""Unit tests for the benchmark baseline branch: force_provider.

These exercise execute_with_fallback with db_session=None — both the stats
snapshot and persistence helpers explicitly tolerate a missing session, so
no database is needed here.
"""

import pytest

from app.core.executor import execute_with_fallback
from app.providers.base import ModelProvider, ProviderResponse
from app.providers.registry import PROVIDER_REGISTRY


class StubProvider(ModelProvider):
    def __init__(self, provider_name: str, respond: ProviderResponse):
        self.provider_name = provider_name
        self.respond = respond
        self.calls = 0

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        self.calls += 1
        return self.respond


def ok(provider_name: str) -> ProviderResponse:
    return ProviderResponse(
        success=True,
        text=f"{provider_name} says hi",
        tokens_used=13,
        latency_ms=42.0,
        provider_name=provider_name,
        model_name="stub-model",
    )


def fail(provider_name: str, error: str = "rate_limit_exceeded") -> ProviderResponse:
    return ProviderResponse(
        success=False,
        text="",
        tokens_used=0,
        latency_ms=25.0,
        error=error,
        provider_name=provider_name,
        model_name="stub-model",
    )


@pytest.fixture
def install_stubs(monkeypatch):
    def _install(**providers: StubProvider):
        for name, stub in providers.items():
            monkeypatch.setitem(PROVIDER_REGISTRY, name, stub)
        return providers

    return _install


async def test_force_provider_attempts_only_that_provider_no_ranking_no_fallback(
    install_stubs,
):
    """force_provider bypasses scoring/ranking and never falls back."""
    gemini = StubProvider("gemini", ok("gemini"))
    groq = StubProvider("groq", ok("groq"))
    deepseek = StubProvider("deepseek", ok("deepseek"))
    install_stubs(gemini=gemini, groq=groq, deepseek=deepseek)

    result = await execute_with_fallback(
        "Say hi",
        db_session=None,
        task_type="code",  # ranking would put deepseek first — bypass it
        max_fallbacks=3,
        force_provider="gemini",
    )

    assert result.success is True
    assert result.model_used == "gemini"
    assert gemini.calls == 1
    assert groq.calls == 0  # never ranked, never attempted
    assert deepseek.calls == 0
    assert len(result.fallback_chain_attempted) == 1
    assert result.fallback_chain_attempted[0].provider_name == "gemini"
    assert result.tokens_used == 13


async def test_force_provider_failure_means_failure_no_fallback(install_stubs):
    """A naive hardcoded deployment has NO fallback: failure is final."""
    gemini = StubProvider("gemini", fail("gemini", error="server_error"))
    groq = StubProvider("groq", ok("groq"))  # healthy and would have served
    install_stubs(gemini=gemini, groq=groq, deepseek=StubProvider("deepseek", ok("deepseek")))

    result = await execute_with_fallback(
        "Say hi",
        db_session=None,
        max_fallbacks=3,  # a naive system ignores this entirely
        force_provider="gemini",
    )

    assert result.success is False
    assert result.model_used is None
    assert result.error == "server_error"
    assert len(result.fallback_chain_attempted) == 1  # exactly one attempt
    assert gemini.calls == 1
    assert groq.calls == 0  # fallback never engaged
