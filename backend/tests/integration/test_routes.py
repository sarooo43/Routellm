"""Phase 7 integration tests — the public REST API surface.

Uses httpx.AsyncClient against the real ASGI app, with the get_session
dependency overridden to the shared in-memory SQLite session, shared
breaker/quota instances injected into the routes module, and stubbed
PROVIDER_REGISTRY entries (no real network calls). The slowapi limiter is
reset after every test so the in-memory per-IP budget never leaks across
test cases.
"""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
import sqlalchemy as sa

from app.api.routes import limiter
from app.core import quota_tracker as quota_module
from app.main import app
from app.models import TaskLog
from tests.integration.conftest import StubProvider, fail, ok


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker):
    from app.api import routes as routes_module
    from app.db.session import get_session

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    monkeypatch.setattr(routes_module, "_get_breaker", lambda: breaker)
    monkeypatch.setattr(routes_module, "_get_quota", lambda: tracker)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    yield
    limiter.reset()


def _task_log(**overrides) -> TaskLog:
    defaults = dict(
        request_id="req-default",
        task_type="code",
        status="success",
        latency_ms=100.0,
        tokens_used=0,
        confidence_score=0.5,
    )
    defaults.update(overrides)
    return TaskLog(**defaults)


# --------------------------------------------------------------------------
# POST /api/v1/route
# --------------------------------------------------------------------------


async def test_post_route_success_exact_shape(client, install_stubs):
    install_stubs(
        groq=StubProvider("groq", [ok("groq", text="Hello!", tokens=21)]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )

    resp = await client.post(
        "/api/v1/route", json={"prompt": "Say hi", "task_type": "code"}
    )

    assert resp.status_code == 200
    data = resp.json()
    assert set(data.keys()) == {
        "success",
        "result",
        "model_used",
        "confidence_score",
        "fallback_chain_attempted",
        "latency_ms",
        "tokens_used",
        "request_id",
        "error",
    }
    assert data["success"] is True
    assert data["result"] == "Hello!"
    assert data["model_used"] == "groq"
    assert data["error"] is None
    assert data["tokens_used"] == 21
    assert data["latency_ms"] > 0
    assert data["request_id"]
    assert len(data["fallback_chain_attempted"]) == 1
    attempt = data["fallback_chain_attempted"][0]
    assert set(attempt.keys()) == {
        "provider_name",
        "success",
        "error",
        "latency_ms",
        "chaos_injected",
    }
    assert attempt["provider_name"] == "groq"
    assert attempt["success"] is True
    assert attempt["chaos_injected"] is False


async def test_post_route_missing_prompt_returns_422(client):
    resp = await client.post("/api/v1/route", json={"task_type": "code"})
    assert resp.status_code == 422

    missing_body = await client.post("/api/v1/route", json={})
    assert missing_body.status_code == 422


async def test_post_route_all_providers_fail_still_200(client, install_stubs):
    install_stubs(
        groq=StubProvider("groq", [fail("groq", error="rate_limit_exceeded")]),
        gemini=StubProvider("gemini", [fail("gemini", error="timeout")]),
        deepseek=StubProvider("deepseek", [fail("deepseek", error="timeout")]),
    )

    resp = await client.post("/api/v1/route", json={"prompt": "hi"})

    assert resp.status_code == 200  # business failure, not an HTTP error
    data = resp.json()
    assert data["success"] is False
    assert data["error"] == "all_providers_failed"
    assert data["model_used"] is None
    assert len(data["fallback_chain_attempted"]) == 3


# --------------------------------------------------------------------------
# GET /api/v1/models
# --------------------------------------------------------------------------


async def test_get_models_mixed_statuses(client, breaker, tracker, monkeypatch):
    from app.api import routes as routes_module

    monkeypatch.setattr(routes_module, "_get_breaker", lambda: breaker)
    monkeypatch.setattr(routes_module, "_get_quota", lambda: tracker)

    # gemini trips OPEN (5 failures), deepseek is HALF_OPEN (failures >= 5,
    # no OPEN state key), groq untouched -> CLOSED.
    for _ in range(5):
        await breaker.record_failure("gemini")
    await breaker.redis.set("circuit:deepseek:failures", 5)

    monkeypatch.setitem(quota_module.DAILY_REQUEST_LIMITS, "deepseek", 10)
    await tracker.record_usage("deepseek", 100)
    await tracker.record_usage("deepseek", 50)

    resp = await client.get("/api/v1/models")
    assert resp.status_code == 200

    body = resp.json()
    assert set(body.keys()) == {"chaos_mode_enabled", "providers"}
    assert body["chaos_mode_enabled"] is False  # flag off in the test env

    items = {item["name"]: item for item in body["providers"]}
    assert set(items.keys()) == {"groq", "gemini", "deepseek"}
    assert all(item["chaos_active"] is False for item in items.values())

    assert items["groq"]["status"] == "healthy"
    assert items["groq"]["requests_today"] == 0
    # Optimistic default when there is no DB history.
    assert items["groq"]["success_rate_overall"] == 0.75
    assert items["groq"]["daily_quota_limit"] == 14400

    assert items["gemini"]["status"] == "circuit_open"

    assert items["deepseek"]["status"] == "degraded"
    assert items["deepseek"]["requests_today"] == 2
    assert items["deepseek"]["daily_quota_limit"] == 10
    assert items["deepseek"]["quota_percentage_used"] == 20.0


# --------------------------------------------------------------------------
# GET /api/v1/logs
# --------------------------------------------------------------------------


async def test_get_logs_pagination_and_filters(client, db_session):
    now = datetime.now(timezone.utc)
    rows = [
        _task_log(request_id="req-1", task_type="code", status="success",
                  created_at=now - timedelta(seconds=2)),
        _task_log(request_id="req-2", task_type="qa", status="failed",
                  error_message="all_providers_failed",
                  created_at=now - timedelta(seconds=1)),
        _task_log(request_id="req-3", task_type="code", status="success",
                  created_at=now - timedelta(seconds=1, milliseconds=500)),
        _task_log(request_id="req-4", task_type="code", status="failed",
                  error_message="provider_server_error",
                  created_at=now - timedelta(milliseconds=500)),
        _task_log(request_id="req-5", task_type="summarization", status="success",
                  created_at=now),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.commit()

    resp = await client.get("/api/v1/logs", params={"limit": 2, "offset": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["logs"]) == 2
    # Ordered newest first (req-2 at -1s is newer than req-3 at -1.5s);
    # offset 1 skips the newest (req-5).
    assert [log["request_id"] for log in body["logs"]] == ["req-4", "req-2"]
    assert body["logs"][0]["created_at"]  # ISO datetime string

    filtered = await client.get("/api/v1/logs", params={"task_type": "code"})
    assert filtered.json()["total"] == 3
    code_ids = {log["request_id"] for log in filtered.json()["logs"]}
    assert code_ids == {"req-1", "req-3", "req-4"}

    status_filtered = await client.get("/api/v1/logs", params={"status": "failed"})
    assert status_filtered.json()["total"] == 2

    combined = await client.get(
        "/api/v1/logs", params={"task_type": "qa", "status": "failed"}
    )
    assert combined.json()["total"] == 1
    assert combined.json()["logs"][0]["request_id"] == "req-2"


async def test_get_logs_empty(client, db_session):
    resp = await client.get("/api/v1/logs")
    assert resp.status_code == 200
    assert resp.json() == {"total": 0, "logs": []}


# --------------------------------------------------------------------------
# GET /api/v1/analytics
# --------------------------------------------------------------------------


async def test_get_analytics_aggregates(client, db_session):
    now = datetime.now(timezone.utc)
    rows = [
        _task_log(request_id="a1", selected_model="groq", tokens_used=1000,
                  latency_ms=100.0, created_at=now),
        _task_log(request_id="a2", selected_model="groq", tokens_used=1000,
                  latency_ms=300.0, created_at=now - timedelta(days=10)),
        _task_log(request_id="a3", selected_model="deepseek", tokens_used=2000,
                  latency_ms=500.0, created_at=now),
        _task_log(request_id="a4", selected_model="gemini", tokens_used=500,
                  latency_ms=250.0, created_at=now),
        _task_log(request_id="a5", status="failed", error_message="all_providers_failed",
                  tokens_used=0, latency_ms=50.0, confidence_score=0.0,
                  created_at=now),
    ]
    for row in rows:
        db_session.add(row)
    await db_session.commit()

    resp = await client.get("/api/v1/analytics")
    assert resp.status_code == 200
    data = resp.json()

    assert data["total_requests"] == 5
    assert data["overall_success_rate"] == 0.8
    assert data["avg_latency_ms"] == 240.0
    # Only deepseek has nonzero cost: 2000/1000 * 0.0002.
    assert data["total_cost_estimate"] == pytest.approx(0.0004)
    # Hypothetical: all 4500 tokens (2000+2000+500) on the most expensive
    # provider (deepseek at 0.0002 per 1k tokens).
    assert data["estimated_cost_if_single_model"] == pytest.approx(0.0009)
    assert data["model_usage_breakdown"] == {"groq": 2, "gemini": 1, "deepseek": 1}

    per_day = data["requests_per_day"]
    assert len(per_day) == 14
    assert sum(entry["count"] for entry in per_day) == 5
    today = now.date().isoformat()
    assert per_day[-1] == {"date": today, "count": 4}
    ten_days_ago = (now - timedelta(days=10)).date().isoformat()
    assert any(e["date"] == ten_days_ago and e["count"] == 1 for e in per_day)


async def test_get_analytics_empty_dashboard_zeroes(client, db_session):
    resp = await client.get("/api/v1/analytics")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_requests"] == 0
    assert data["overall_success_rate"] == 0.0
    assert data["avg_latency_ms"] == 0.0
    assert data["total_cost_estimate"] == 0.0
    assert data["model_usage_breakdown"] == {"groq": 0, "gemini": 0, "deepseek": 0}


# --------------------------------------------------------------------------
# Rate limiting on POST /api/v1/route
# --------------------------------------------------------------------------


async def test_rate_limit_kicks_in_after_31_requests(client, install_stubs):
    install_stubs(
        groq=StubProvider("groq", [ok("groq")]),

    )
    statuses = []
    for _ in range(31):
        resp = await client.post("/api/v1/route", json={"prompt": "ping"})
        statuses.append(resp.status_code)

    assert statuses[:30] == [200] * 30
    assert statuses[30] == 429


# --------------------------------------------------------------------------
# /api/v1/health
# --------------------------------------------------------------------------


async def test_health_returns_ok_shape(client):
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in {"ok", "degraded"}
    assert body["service"] == "routellm-backend"
    assert "redis" in body["components"]
    assert "database" in body["components"]
