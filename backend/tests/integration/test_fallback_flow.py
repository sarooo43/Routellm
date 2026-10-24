import pytest

from app.core.executor import execute_with_fallback
from tests.integration.conftest import (
    CAPABILITY_ONLY_WEIGHTS,
    RecordingBreaker,
    StubProvider,
    expected_ranking,
    fail,
    ok,
)


# ------------------------- Scenario A: happy path -------------------------


async def test_happy_path_first_choice_succeeds(
    install_stubs, breaker, tracker, db_session
):
    stubs = install_stubs(
        groq=StubProvider("groq", [ok("groq")]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    order = expected_ranking("code")

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    assert result.success is True
    assert result.model_used == order[0]  # groq
    assert stubs[order[0]].calls == 1
    assert all(stubs[n].calls == 0 for n in order[1:])
    assert len(result.fallback_chain_attempted) == 1
    assert result.fallback_chain_attempted[0].success is True
    assert result.result_text == f"{order[0]} says hi"
    assert result.tokens_used == 11
    assert result.total_latency_ms > 0
    assert result.error is None
    assert result.request_id  # UUID was minted
    assert breaker.failure_calls == []
    assert breaker.success_calls == [order[0]]


async def test_task_type_auto_classified_when_omitted(
    install_stubs, breaker, tracker, db_session
):
    # "Write a python function..." classifies as "code" without override.
    stubs = install_stubs(
        groq=StubProvider("groq", [ok("groq")]),
        gemini=StubProvider("gemini", [fail("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    order = expected_ranking("code")
    result = await execute_with_fallback(
        "Write a python function to add two numbers",
        db_session=db_session,
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )
    assert result.success is True
    assert result.model_used == order[0]
    assert stubs[order[0]].calls == 1


# ---------------------- Scenario B: one fallback needed ----------------------


async def test_one_fallback_after_failure(install_stubs, breaker, tracker, db_session):
    # Corrected default-weights math for "code": groq 0.717 > deepseek 0.675 >
    # gemini 0.648. groq fails -> deepseek (2nd) succeeds; gemini never reached.
    groq = StubProvider("groq", [fail("groq")])
    deepseek = StubProvider("deepseek", [ok("deepseek")])
    gemini = StubProvider("gemini", [fail("gemini")])
    install_stubs(groq=groq, deepseek=deepseek, gemini=gemini)
    order = expected_ranking("code")  # [groq, deepseek, gemini]

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    assert result.success is True
    assert result.model_used == order[1]
    assert groq.calls == 1
    assert gemini.calls == 0

    chain = result.fallback_chain_attempted
    assert len(chain) == 2
    assert chain[0].provider_name == order[0]
    assert chain[0].success is False
    assert chain[0].error == "rate_limit_exceeded"
    assert chain[1].provider_name == order[1]
    assert chain[1].success is True

    # Breaker saw exactly one failure (groq) and one success (deepseek).
    assert breaker.failure_calls == [order[0]]
    assert breaker.success_calls == [order[1]]


# ------------------------ Scenario C: total failure ------------------------


async def test_all_providers_failed(install_stubs, breaker, tracker, db_session):
    stubs = install_stubs(
        groq=StubProvider("groq", [fail("groq")]),
        gemini=StubProvider("gemini", [fail("gemini", error="timeout")]),
        deepseek=StubProvider("deepseek", [fail("deepseek", error="auth_failed")]),
    )
    order = expected_ranking("code")

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    assert result.success is False
    assert result.error == "all_providers_failed"
    assert result.model_used is None
    assert result.result_text == ""
    assert [a.provider_name for a in result.fallback_chain_attempted] == order
    assert len(result.fallback_chain_attempted) == 3
    assert all(a.success is False for a in result.fallback_chain_attempted)
    assert sorted(breaker.failure_calls) == sorted(order)
    assert result.total_latency_ms > 0


# ---------------------- Scenario D: circuit-open skip ----------------------


async def test_circuit_open_provider_skipped_without_call(
    install_stubs, breaker, tracker, db_session
):
    """Trip deepseek's circuit, then verify it is never called.

    Note: Phase 3's guarantee ("OPEN always ranks last regardless of score")
    means a circuit-open provider can never be encountered BEFORE healthy
    providers in a 3-provider registry - the breaker's ranking effect drops
    it below gemini in the snapshot (0.575 < 0.648). So the observable flow
    is: groq fails -> gemini fails -> deepseek SKIPPED with a circuit_open
    log and zero generate() calls. That it was never called even in the
    fallback chain is the resilience behavior we want to prove.
    """
    for _ in range(5):
        await breaker.record_failure("deepseek")
    assert await breaker.get_state("deepseek") == "OPEN"

    deepseek = StubProvider("deepseek", [ok("deepseek")])
    groq = StubProvider("groq", [fail("groq")])
    gemini = StubProvider("gemini", [fail("gemini", error="timeout")])
    install_stubs(groq=groq, gemini=gemini, deepseek=deepseek)

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    # The circuit-open provider's generate() was NEVER invoked.
    assert deepseek.calls == 0
    assert groq.calls == 1
    assert gemini.calls == 1

    chain = result.fallback_chain_attempted
    assert [a.provider_name for a in chain] == ["groq", "gemini", "deepseek"]
    skipped = chain[-1]
    assert skipped.provider_name == "deepseek"
    assert skipped.success is False
    assert skipped.error == "circuit_open"
    assert skipped.latency_ms == 0.0

    assert result.success is False
    assert result.error == "all_providers_failed"
    # Skipping must not record any NEW breaker failure for deepseek: exactly
    # the 5 setup calls that tripped it, then nothing during execution.
    assert breaker.failure_calls.count("deepseek") == 5
    assert breaker.failure_calls.count("groq") == 1
    assert breaker.failure_calls.count("gemini") == 1


async def test_skips_do_not_consume_fallback_budget(
    install_stubs, breaker, tracker, db_session, monkeypatch
):
    """A gated provider (quota) logs a skip entry but consumes NO budget:
    with max_fallbacks=1, the skip still leaves room for one real call,
    which succeeds."""
    from app.core import quota_tracker as quota_tracker_module

    monkeypatch.setitem(quota_tracker_module.DAILY_REQUEST_LIMITS, "deepseek", 1)
    await tracker.record_usage("deepseek")

    deepseek = StubProvider("deepseek", [ok("deepseek")])
    groq = StubProvider("groq", [ok("groq")])
    install_stubs(
        groq=groq,
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=deepseek,
    )

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        max_fallbacks=1,
        circuit_breaker=breaker,
        quota_tracker=tracker,
        weights=CAPABILITY_ONLY_WEIGHTS,  # deepseek (quota-blocked) ranks first
    )

    assert result.success is True
    assert result.model_used == "groq"
    assert deepseek.calls == 0
    assert groq.calls == 1
    # One skip entry + one successful attempt, despite max_fallbacks=1 -
    # proves the skip did not count against the fallback budget.
    assert len(result.fallback_chain_attempted) == 2
    assert result.fallback_chain_attempted[0].error == "quota_exceeded"
    assert result.fallback_chain_attempted[1].success is True


# ---------------------- Scenario E: quota-exceeded skip ----------------------


async def test_quota_exceeded_provider_skipped_without_call(
    install_stubs, breaker, tracker, db_session, monkeypatch
):
    from app.core import quota_tracker as quota_tracker_module

    monkeypatch.setitem(quota_tracker_module.DAILY_REQUEST_LIMITS, "deepseek", 1)
    await tracker.record_usage("deepseek")  # reaches its tiny limit
    assert await tracker.is_within_quota("deepseek") is False

    deepseek = StubProvider("deepseek", [ok("deepseek")])
    groq = StubProvider("groq", [ok("groq")])
    gemini = StubProvider("gemini", [ok("gemini")])
    install_stubs(groq=groq, gemini=gemini, deepseek=deepseek)

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
        weights=CAPABILITY_ONLY_WEIGHTS,
    )

    assert result.success is True
    assert result.model_used == "groq"
    # THE proof: quota-exhausted provider's generate() was never invoked.
    assert deepseek.calls == 0
    assert groq.calls == 1
    chain = result.fallback_chain_attempted
    assert chain[0].error == "quota_exceeded"
    assert chain[0].provider_name == "deepseek"
    assert chain[1].provider_name == "groq"
    assert chain[1].success is True


# --------------------- Scenario F: max_fallbacks respected ---------------------


async def test_max_fallbacks_limit_respected(install_stubs, breaker, tracker, db_session):
    groq = StubProvider("groq", [fail("groq")])
    gemini = StubProvider("gemini", [ok("gemini")])
    deepseek = StubProvider("deepseek", [ok("deepseek")])
    install_stubs(groq=groq, gemini=gemini, deepseek=deepseek)

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        max_fallbacks=1,
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    assert result.success is False
    assert result.error == "all_providers_failed"
    assert len(result.fallback_chain_attempted) == 1
    assert groq.calls == 1
    assert gemini.calls == 0  # never tried: limit reached
    assert deepseek.calls == 0


async def test_max_fallbacks_zero_treated_as_one(
    install_stubs, breaker, tracker, db_session
):
    groq = StubProvider("groq", [fail("groq")])
    gemini = StubProvider("gemini", [ok("gemini")])
    install_stubs(
        groq=groq,
        gemini=gemini,
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        max_fallbacks=0,
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )
    assert result.success is False
    assert len(result.fallback_chain_attempted) == 1
    assert groq.calls == 1
    assert gemini.calls == 0


# ---------------------- DeepSeek model_variant dispatch ----------------------


async def test_deepseek_gets_coder_variant_for_code_task(
    install_stubs, breaker, tracker, db_session
):
    deepseek = StubProvider("deepseek", [ok("deepseek")])
    install_stubs(
        groq=StubProvider("groq", [fail("groq")]),
        gemini=StubProvider("gemini", [fail("gemini")]),
        deepseek=deepseek,
    )
    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )
    assert result.success is True
    assert result.model_used == "deepseek"
    assert deepseek.kwargs_seen == [{"model_variant": "deepseek-coder"}]


async def test_deepseek_gets_chat_variant_for_general_task(
    install_stubs, breaker, tracker, db_session
):
    deepseek = StubProvider("deepseek", [ok("deepseek")])
    install_stubs(
        groq=StubProvider("groq", [fail("groq")]),
        gemini=StubProvider("gemini", [fail("gemini")]),
        deepseek=deepseek,
    )
    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="general",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )
    assert result.success is True
    assert deepseek.kwargs_seen == [{"model_variant": "deepseek-chat"}]
