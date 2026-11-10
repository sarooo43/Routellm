"""What a failed status does to the shared circuit breaker.

Two rules, both decided deliberately and both asserted here because they are
easy to get subtly wrong:

1. **401/403 from the caller's own BYOK key never opens the global breaker.**
   One user pasting a revoked key must not lock the provider out for everyone.
2. **400/404 do not count as failures either — but the provider actually
   answered, so they are recorded as a success.** A retired model name is our
   config bug; it is not evidence the provider is down. Before this task those
   responses opened the breaker *and* were reported as auth failures.
"""

import pytest

from app.core import executor as executor_module
from app.core.executor import execute_with_fallback
from tests.integration.conftest import StubProvider, ok


async def _run(install_stubs, breaker, tracker, db_session, response):
    stub = StubProvider("groq", [response])
    install_stubs(
        groq=stub,
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    # NOT force_provider: that path is the deliberately naive baseline and has
    # no breaker side effects at all. The ranked path is the one that owns the
    # breaker, and for task_type="code" groq ranks first.
    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )
    return stub, result


def _config_error(outcome: str, detail: str):
    from app.providers.base import ProviderResponse

    return ProviderResponse(
        success=False,
        error=f"{outcome}: {detail}",
        outcome=outcome,
        provider_name="groq",
        model_name="llama-3.3-70b-versatile",
        latency_ms=12.0,
    )


async def test_404_does_not_open_the_breaker(install_stubs, breaker, tracker, db_session):
    _, result = await _run(
        install_stubs,
        breaker,
        tracker,
        db_session,
        _config_error(
            "model_not_found",
            "groq (api.groq.com) has no model named 'llama-3.3-70b-versatile'. "
            "It may have been retired.",
        ),
    )

    # It failed over, but for an honest reason.
    assert result.fallback_chain_attempted[0].provider_name == "groq"
    assert result.fallback_chain_attempted[0].error.startswith("model_not_found")
    assert "groq" not in breaker.failure_calls
    # The provider answered, so the breaker is told the provider is alive.
    assert "groq" in breaker.success_calls


async def test_400_does_not_open_the_breaker(install_stubs, breaker, tracker, db_session):
    await _run(
        install_stubs,
        breaker,
        tracker,
        db_session,
        _config_error("bad_request", "groq (api.groq.com) rejected the request as malformed"),
    )
    assert "groq" not in breaker.failure_calls


@pytest.mark.parametrize("outcome", ["rate_limit_exceeded", "provider_server_error", "timeout"])
async def test_real_failures_still_count(install_stubs, breaker, tracker, db_session, outcome):
    await _run(
        install_stubs,
        breaker,
        tracker,
        db_session,
        _config_error(outcome, "groq fell over"),
    )
    assert breaker.failure_calls.count("groq") == 1


async def test_user_key_auth_error_does_not_open_the_breaker(
    install_stubs, breaker, tracker, db_session
):
    """BYOK 401 is the caller's key, not an outage for everyone else."""
    stub = StubProvider(
        "groq", [_config_error("auth_failed", "groq rejected the credential (HTTP 401)")]
    )
    install_stubs(groq=stub, gemini=StubProvider("gemini", [ok("gemini")]), deepseek=StubProvider("deepseek", [ok("deepseek")]))

    await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
        provider_api_keys={"groq": "gsk_test_key_123456"},
    )
    assert "groq" not in breaker.failure_calls


async def test_config_errors_are_logged_as_such(
    install_stubs, breaker, tracker, db_session, capsys
):
    """The operator needs to see why the breaker did not move."""
    await _run(
        install_stubs,
        breaker,
        tracker,
        db_session,
        _config_error("model_not_found", "groq has no model named 'x'"),
    )
    # The app's logger writes JSON lines to stdout, not through stdlib logging,
    # so caplog sees nothing; assert on captured output instead.
    out = capsys.readouterr().out
    assert "breaker_skipped_config_error" in out
    assert "not an outage" in out


def test_config_error_outcomes_are_exactly_the_4xx_client_errors():
    assert executor_module.CONFIG_ERROR_OUTCOMES == frozenset(
        {"bad_request", "model_not_found", "endpoint_not_found", "not_found"}
    )
    # Auth failures are deliberately NOT in this set: they are handled by the
    # BYOK rule above, and a 401 from the *server's own* key is a real outage
    # signal that should be allowed to open the breaker.
    assert "auth_failed" not in executor_module.CONFIG_ERROR_OUTCOMES
