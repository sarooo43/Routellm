import pytest
from sqlalchemy import select

from app.core.executor import execute_with_fallback
from app.models import ModelStatsDB, TaskLog, UsageTracking
from datetime import datetime, timezone
from tests.integration.conftest import StubProvider, expected_ranking, fail, ok


async def _all_task_logs(session):
    return (await session.execute(select(TaskLog))).scalars().all()


async def _rows_of(session, model):
    return (await session.execute(select(model))).scalars().all()


async def test_success_creates_single_task_log(
    install_stubs, breaker, tracker, db_session
):
    order = expected_ranking("code")
    stubs = {
        order[0]: StubProvider(order[0], [ok(order[0])]),
        order[1]: StubProvider(order[1], [ok(order[1])]),
        order[2]: StubProvider(order[2], [ok(order[2])]),
    }
    install_stubs(**stubs)

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )
    assert result.success is True

    rows = await _all_task_logs(db_session)
    assert len(rows) == 1
    log = rows[0]
    assert log.status == "success"
    assert log.selected_model == order[0]
    assert log.task_type == "code"
    assert log.confidence_score == result.confidence_score
    assert log.request_id == result.request_id
    assert log.tokens_used == 11
    assert log.error_message is None
    assert len(log.fallback_chain) == 1
    assert log.fallback_chain[0]["provider_name"] == order[0]
    assert log.fallback_chain[0]["success"] is True


async def test_fallback_chain_persisted_in_order(
    install_stubs, breaker, tracker, db_session
):
    groq = StubProvider("groq", [fail("groq")])
    deepseek = StubProvider("deepseek", [ok("deepseek")])
    gemini = StubProvider("gemini", [fail("gemini")])
    install_stubs(groq=groq, deepseek=deepseek, gemini=gemini)

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )
    assert result.model_used == "deepseek"

    (log,) = await _all_task_logs(db_session)
    chain = log.fallback_chain
    assert len(chain) == 2
    assert chain[0]["provider_name"] == "groq"
    assert chain[0]["success"] is False
    assert chain[0]["error"] == "rate_limit_exceeded"
    assert chain[1]["provider_name"] == "deepseek"
    assert chain[1]["success"] is True
    assert log.selected_model == "deepseek"
    assert log.status == "success"


async def test_all_failed_creates_failed_log(
    install_stubs, breaker, tracker, db_session
):
    install_stubs(
        groq=StubProvider("groq", [fail("groq")]),
        gemini=StubProvider("gemini", [fail("gemini", error="timeout")]),
        deepseek=StubProvider("deepseek", [fail("deepseek", error="auth_failed")]),
    )

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )
    assert result.success is False

    (log,) = await _all_task_logs(db_session)
    assert log.status == "failed"
    assert log.selected_model is None
    assert log.error_message == "all_providers_failed"
    assert log.fallback_chain == [
        log_entry.model_dump() for log_entry in result.fallback_chain_attempted
    ]


async def test_model_stats_created_on_first_request(
    install_stubs, breaker, tracker, db_session
):
    order = expected_ranking("code")
    install_stubs(
        groq=StubProvider("groq", [ok("groq", latency=50.0)]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )

    await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    rows = (
        await _rows_of(db_session, ModelStatsDB)
    )
    targeted = [r for r in rows if r.provider_name == "groq" and r.task_type == "code"]
    assert len(targeted) == 1  # unique (provider, task) row
    row = targeted[0]
    assert row.total_requests == 1
    assert row.successful_requests == 1
    assert row.success_rate == 1.0
    assert row.avg_latency_ms == pytest.approx(50.0)


async def test_model_stats_increment_and_recompute(
    install_stubs, breaker, tracker, db_session
):
    install_stubs(
        groq=StubProvider("groq", [ok("groq", latency=50.0)]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )

    await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    # Second request, different latency: running average must update to
    # ((50 * 1) + 150) / 2 = 100.0
    install_stubs(
        groq=StubProvider("groq", [ok("groq", latency=150.0)]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    rows = await _rows_of(db_session, ModelStatsDB)
    groq_rows = [r for r in rows if r.provider_name == "groq"]
    assert len(groq_rows) == 1
    row = groq_rows[0]
    assert row.total_requests == 2
    assert row.successful_requests == 2
    assert row.success_rate == 1.0
    assert row.avg_latency_ms == pytest.approx(100.0)


async def test_model_stats_tracks_failures_too(
    install_stubs, breaker, tracker, db_session
):
    # Call 1: groq fails (latency 25), deepseek (2nd) succeeds.
    install_stubs(
        groq=StubProvider("groq", [fail("groq", latency=25.0)]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
        gemini=StubProvider("gemini", [ok("gemini")]),
    )
    await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    rows = await _rows_of(db_session, ModelStatsDB)
    by_provider = {r.provider_name: r for r in rows}
    groq = by_provider["groq"]
    assert groq.total_requests == 1
    assert groq.successful_requests == 0
    assert groq.success_rate == 0.0
    assert groq.avg_latency_ms == pytest.approx(25.0)


async def test_usage_tracking_accumulates_same_day(
    install_stubs, breaker, tracker, db_session
):
    install_stubs(
        groq=StubProvider("groq", [ok("groq", tokens=11)]),
        gemini=StubProvider("gemini", [ok("gemini", tokens=7)]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )
    # Second call, same day: counters accumulate in the same row.
    install_stubs(
        groq=StubProvider("groq", [ok("groq", tokens=5)]),
        gemini=StubProvider("gemini", [ok("gemini", tokens=7)]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    rows = await _rows_of(db_session, UsageTracking)
    groq_rows = [
        r
        for r in rows
        if r.provider_name == "groq"
        and r.date == datetime.now(timezone.utc).date()
    ]
    assert len(groq_rows) == 1
    assert groq_rows[0].requests_count == 2
    assert groq_rows[0].tokens_used == 16  # 11 + 5


async def test_db_stats_actually_influence_routing(
    install_stubs, breaker, tracker, db_session
):
    """Pre-seed deepseek/code with a toxic history: success 0/10, slow.
    Even though deepseek has capability 1.0 on code, real stats must demote
    it below gemini (0.45 < 0.648), so the router falls past groq to gemini
    instead of to deepseek."""
    db_session.add(
        ModelStatsDB(
            provider_name="deepseek",
            task_type="code",
            total_requests=10,
            successful_requests=0,
            avg_latency_ms=900.0,
            success_rate=0.0,
        )
    )
    await db_session.commit()

    groq = StubProvider("groq", [fail("groq")])
    gemini = StubProvider("gemini", [ok("gemini")])
    deepseek = StubProvider("deepseek", [ok("deepseek")])
    install_stubs(groq=groq, gemini=gemini, deepseek=deepseek)

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
    )

    assert result.success is True
    assert result.model_used == "gemini"
    # Proof the DB row (not optimistic defaults) drove the decision:
    # with no history, deepseek (0.675) would have been tried before gemini,
    # and with fresh optimistic defaults gemini would never be reached here.
    assert deepseek.calls == 0
    assert gemini.calls == 1


