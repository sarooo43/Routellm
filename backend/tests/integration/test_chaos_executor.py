"""Chaos-mode integration tests — executor rerouting + admin endpoint gating.

Scenarios:
- with chaos injected on the TOP-ranked provider, execute_with_fallback()
  NEVER calls that provider's real generate() and falls through to the next
  provider through the ordinary executor path;
- the attempt log carries error="chaos_injected_failure" (distinct from real
  failures like rate_limit_exceeded) and chaos_injected=True;
- chaos failures feed the REAL circuit breaker (repeated injections trip it);
- chaos attempts do NOT poison permanent history (ModelStats/UsageTracking);
- /api/v1/admin/chaos/* returns 404 when CHAOS_MODE_ENABLED=False (default),
  and works end-to-end when explicitly enabled.
"""

import httpx
import pytest

from app.config import settings
from tests.integration.conftest import (
    CAPABILITY_ONLY_WEIGHTS,
    StubProvider,
    expected_ranking,
    ok,
)

# --------------------------------------------------------------------------
# Executor: chaos skips the provider call, falls back through real logic
# --------------------------------------------------------------------------


async def test_chaos_top_provider_never_called_falls_back(
    install_stubs, breaker, tracker, db_session, monkeypatch, redis_client
):
    monkeypatch.setattr(settings, "CHAOS_MODE_ENABLED", True)
    from app.core.chaos import ChaosController

    chaos_controller = ChaosController(redis_client)
    order = expected_ranking("code", CAPABILITY_ONLY_WEIGHTS)
    top_provider = order[0]

    stubs: dict[str, StubProvider] = {}
    for name in order:
        stubs[name] = StubProvider(name, [ok(name)])
    install_stubs(**stubs)
    await chaos_controller.inject_failure(top_provider, duration_seconds=60)

    from app.core.executor import execute_with_fallback

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
        weights=CAPABILITY_ONLY_WEIGHTS,
        chaos_controller=chaos_controller,
    )

    # THE proof: the chaos-injected provider's real API was never touched.
    assert stubs[top_provider].calls == 0
    # Fallback happened through the ordinary executor logic, not a UI fake-out.
    assert result.success is True
    assert result.model_used != top_provider
    assert result.model_used == order[1]

    chain = result.fallback_chain_attempted
    assert chain[0].provider_name == top_provider
    assert chain[0].error == "chaos_injected_failure"
    assert chain[0].chaos_injected is True
    assert chain[1].provider_name == order[1]
    assert chain[1].success is True
    assert chain[1].chaos_injected is False


async def test_chaos_attempt_error_log_is_distinguishable_from_real_failures(
    install_stubs, breaker, tracker, db_session, monkeypatch, redis_client
):
    monkeypatch.setattr(settings, "CHAOS_MODE_ENABLED", True)
    from app.core.chaos import ChaosController
    from app.core.executor import execute_with_fallback

    chaos_controller = ChaosController(redis_client)
    order = expected_ranking("code", CAPABILITY_ONLY_WEIGHTS)
    top_provider = order[0]

    from tests.integration.conftest import fail

    real_failure_provider = order[1]
    stubs = {
        name: StubProvider(
            name,
            [fail(name, error="rate_limit_exceeded")] if name == real_failure_provider else [ok(name)],
        )
        for name in order
    }
    install_stubs(**stubs)
    await chaos_controller.inject_failure(top_provider, duration_seconds=60)

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
        weights=CAPABILITY_ONLY_WEIGHTS,
        chaos_controller=chaos_controller,
        max_fallbacks=3,
    )

    chain = result.fallback_chain_attempted
    errors = {entry.provider_name: (entry.error, entry.chaos_injected) for entry in chain}
    # Artificial failure is labeled chaos; real provider error stays a real one.
    assert errors[top_provider] == ("chaos_injected_failure", True)
    assert errors[real_failure_provider][0] == "rate_limit_exceeded"
    assert errors[real_failure_provider][1] is False


async def test_repeated_chaos_injections_trip_real_circuit_breaker(
    install_stubs, breaker, tracker, db_session, monkeypatch, redis_client
):
    monkeypatch.setattr(settings, "CHAOS_MODE_ENABLED", True)
    from app.core import circuit_breaker as circuit_breaker_module
    from app.core.chaos import ChaosController
    from app.core.executor import execute_with_fallback

    chaos_controller = ChaosController(redis_client)
    order = expected_ranking("code", CAPABILITY_ONLY_WEIGHTS)
    top_provider = order[0]

    stubs = {name: StubProvider(name, [ok(name) for _ in range(10)]) for name in order}
    install_stubs(**stubs)
    await chaos_controller.inject_failure(top_provider, duration_seconds=3600)

    results = [
        await execute_with_fallback(
            "Say hi",
            db_session=db_session,
            task_type="code",
            circuit_breaker=breaker,
            quota_tracker=tracker,
            weights=CAPABILITY_ONLY_WEIGHTS,
            chaos_controller=chaos_controller,
        )
        for _ in range(circuit_breaker_module.FAILURE_THRESHOLD)
    ]

    # Every execution fed the chaos failure into the REAL breaker...
    assert breaker.failure_calls.count(top_provider) >= 1
    # ...until it actually tripped OPEN.
    assert (await breaker.get_state(top_provider)) == "OPEN"

    # Next request: the (real, now-learned) OPEN breaker makes the scorer rank
    # this provider LAST, so the loop spends its budget on the healthy
    # providers first and never reaches it — the ordinary circuit-outage path.
    final = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
        weights=CAPABILITY_ONLY_WEIGHTS,
        chaos_controller=chaos_controller,
    )
    final_chain = final.fallback_chain_attempted
    assert all(entry.provider_name != top_provider for entry in final_chain)
    assert final.success is True
    # Across the WHOLE test (5 injections + breaker OPEN) the provider's real
    # API was never once called — chaos never had to.
    assert stubs[top_provider].calls == 0
    assert all(res.success for res in results)  # demo requests always succeeded


async def test_chaos_attempts_do_not_poison_permanent_history(
    install_stubs, breaker, tracker, db_session, monkeypatch, redis_client
):
    monkeypatch.setattr(settings, "CHAOS_MODE_ENABLED", True)
    from app.core.chaos import ChaosController
    from app.core.executor import execute_with_fallback

    chaos_controller = ChaosController(redis_client)
    order = expected_ranking("code", CAPABILITY_ONLY_WEIGHTS)
    top_provider = order[0]
    stubs = {name: StubProvider(name, [ok(name)]) for name in order}
    install_stubs(**stubs)
    await chaos_controller.inject_failure(top_provider, duration_seconds=60)

    await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
        weights=CAPABILITY_ONLY_WEIGHTS,
        chaos_controller=chaos_controller,
    )

    from sqlalchemy import select

    from app.models import ModelStatsDB, UsageTracking

    rows = (
        (await db_session.execute(select(ModelStatsDB))).scalars().all()
    )
    # Only the REAL succeeding provider got a stats row; chaos left none.
    assert [row.provider_name for row in rows] == [order[1]]
    usage_rows = (await db_session.execute(select(UsageTracking))).scalars().all()
    assert [row.provider_name for row in usage_rows] == [order[1]]


async def test_chaos_gate_inert_when_mode_disabled(
    install_stubs, breaker, tracker, db_session, monkeypatch, redis_client
):
    # Flag explicitly False (default): stale chaos keys from an earlier demo
    # session must have ZERO effect on routing.
    monkeypatch.setattr(settings, "CHAOS_MODE_ENABLED", False)
    from app.core.chaos import ChaosController
    from app.core.executor import execute_with_fallback

    chaos_controller = ChaosController(redis_client)
    order = expected_ranking("code", CAPABILITY_ONLY_WEIGHTS)
    top_provider = order[0]
    stubs = {name: StubProvider(name, [ok(name)]) for name in order}
    install_stubs(**stubs)
    await chaos_controller.inject_failure(top_provider, duration_seconds=60)
    assert await chaos_controller.is_chaos_active(top_provider) is True  # key exists

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="code",
        circuit_breaker=breaker,
        quota_tracker=tracker,
        weights=CAPABILITY_ONLY_WEIGHTS,
        chaos_controller=chaos_controller,
    )

    assert result.success is True
    assert stubs[top_provider].calls == 1  # real call went out
    chain = result.fallback_chain_attempted
    assert all(entry.error is None for entry in chain)


async def test_chaos_injected_forced_provider_fails_without_fallback(
    install_stubs, breaker, tracker, db_session, monkeypatch, redis_client
):
    """The benchmark's naive arm: force_provider onto a chaos-downed provider
    fails outright (zero fallback), while the healthy providers stay intact."""
    monkeypatch.setattr(settings, "CHAOS_MODE_ENABLED", True)
    from app.core.chaos import ChaosController
    from app.core.executor import execute_with_fallback

    chaos_controller = ChaosController(redis_client)
    gemini = StubProvider("gemini", ok("gemini"))
    groq = StubProvider("groq", ok("groq"))
    install_stubs(gemini=gemini, groq=groq)
    await chaos_controller.inject_failure("gemini", duration_seconds=60)

    result = await execute_with_fallback(
        "Say hi",
        db_session=db_session,
        task_type="qa",
        max_fallbacks=3,
        circuit_breaker=breaker,
        quota_tracker=tracker,
        chaos_controller=chaos_controller,
        force_provider="gemini",
    )

    assert result.success is False
    assert result.model_used is None
    assert result.error == "chaos_injected_failure"
    assert gemini.calls == 0  # failure synthesized, not a real API call
    assert groq.calls == 0  # naive mode never falls back
    assert len(result.fallback_chain_attempted) == 1
    assert result.fallback_chain_attempted[0].chaos_injected is True


# --------------------------------------------------------------------------
# API: /api/v1/admin/chaos/* gating
# --------------------------------------------------------------------------


async def test_chaos_admin_routes_404_when_disabled(client_default_app):
    # Default (production-safe) app: chaos router was never mounted, so the
    # endpoints answer FastAPI's native 404 — exactly like unknown paths.
    for method, path in (
        ("post", "/api/v1/admin/chaos/inject"),
        ("post", "/api/v1/admin/chaos/clear"),
        ("get", "/api/v1/admin/chaos/status"),
    ):
        resp = await client_default_app.request(method, path)
        assert resp.status_code == 404
    injected = await client_default_app.post(
        "/api/v1/admin/chaos/inject",
        json={"provider_name": "groq"},
    )
    assert injected.status_code == 404


@pytest.fixture
async def client_default_app(db_session):
    """httpx client against the default app with chaos flag OFF."""
    from app.main import app

    assert settings.CHAOS_MODE_ENABLED is False  # production-safe default
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_chaos_admin_routes_work_when_explicitly_enabled(
    monkeypatch, redis_client
):
    # Standalone ASGI app mounting ONLY the chaos router, as routes.py does
    # when settings.CHAOS_MODE_ENABLED is true at startup.
    from fastapi import FastAPI

    from app.api import chaos_routes as chaos_module
    from app.api.chaos_routes import chaos_router
    from app.core.chaos import ChaosController
    from app.providers.registry import PROVIDER_REGISTRY

    controller = ChaosController(redis_client)
    monkeypatch.setattr(chaos_module, "_get_chaos", lambda: controller)
    assert list(PROVIDER_REGISTRY)  # sanity: at least one real provider name
    first_provider = next(iter(PROVIDER_REGISTRY))

    demo_app = FastAPI()
    demo_app.include_router(chaos_router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=demo_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        inject = await ac.post(
            "/api/v1/admin/chaos/inject",
            json={"provider_name": first_provider, "duration_seconds": 120},
        )
        assert inject.status_code == 200
        assert inject.json() == {
            "status": "injected",
            "provider_name": first_provider,
            "expires_in_seconds": 120,
        }

        status = await ac.get("/api/v1/admin/chaos/status")
        assert status.status_code == 200
        assert status.json() == {"active_chaos": [first_provider]}

        cleared = await ac.post(
            "/api/v1/admin/chaos/clear",
            json={"provider_name": first_provider},
        )
        assert cleared.status_code == 200
        assert cleared.json() == {"status": "cleared", "provider_name": first_provider}

        status_after = await ac.get("/api/v1/admin/chaos/status")
        assert status_after.json() == {"active_chaos": []}

        unknown = await ac.post(
            "/api/v1/admin/chaos/inject",
            json={"provider_name": "not_a_provider"},
        )
        assert unknown.status_code == 400
