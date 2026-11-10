"""The log side of the no-leak rule, on the real routing path.

Kept in ``tests/integration`` because it needs the stubbed-provider and
database fixtures; the pure redaction sweeps live in
``tests/unit/test_no_secret_leaks.py``.
"""

from app.providers.base import ProviderResponse
from app.providers.outcomes import redact

from tests.unit.test_no_secret_leaks import SECRETS, _assert_clean


async def test_logs_written_during_a_failed_route_are_clean(
    install_stubs, breaker, tracker, db_session, capsys
):
    """The app logger writes JSON to stdout: capture and search all of it."""
    from app.core.executor import execute_with_fallback
    from tests.integration.conftest import StubProvider, ok

    secret = SECRETS["groq"]
    failing = ProviderResponse(
        success=False,
        error=f"auth_failed: groq rejected the credential (HTTP 401). Provider said: bad key {redact(secret)}",
        outcome="auth_failed",
        provider_name="groq",
        model_name="llama-3.1-8b-instant",
        latency_ms=5.0,
    )
    install_stubs(
        groq=StubProvider("groq", [failing]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
        provider_api_keys={"groq": secret},
    )

    captured = capsys.readouterr()
    _assert_clean(captured.out + captured.err)
    _assert_clean(result.model_dump_json())
