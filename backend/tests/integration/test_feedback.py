"""Integration tests — human-feedback endpoints and their effect on routing.

Uses the shared in-memory SQLite DB (Feedback table registered via
app.models imports) and the same ASGI-client pattern as test_routes.py.
"""

import httpx
import pytest
from sqlmodel import select

from app.api.routes import limiter
from app.core.executor import _build_stats_snapshot
from app.main import app
from app.models import Feedback, TaskLog
from tests.integration.conftest import StubProvider, ok


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client, auth_headers):
    from app.api import routes as routes_module
    from app.core import executor as executor_module
    from app.core.chaos import ChaosController
    from app.db.session import get_session

    async def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    monkeypatch.setattr(routes_module, "_get_breaker", lambda: breaker)
    monkeypatch.setattr(routes_module, "_get_quota", lambda: tracker)
    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", headers=auth_headers
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    yield
    limiter.reset()


def _seed_log(session, request_id: str, status: str = "success", task_type: str = "summarization", selected_model: str | None = "groq") -> TaskLog:
    log = TaskLog(
        request_id=request_id,
        task_type=task_type,
        selected_model=selected_model,
        confidence_score=0.8,
        status=status,
        latency_ms=100.0,
        tokens_used=11,
    )
    session.add(log)
    return log


# --------------------------------------------------------------------------
# POST /api/v1/feedback
# --------------------------------------------------------------------------


async def test_feedback_on_valid_successful_request_returns_201(client, db_session):
    _seed_log(db_session, "req-ok")
    await db_session.commit()

    resp = await client.post(
        "/api/v1/feedback",
        json={"request_id": "req-ok", "rating": "positive"},
    )
    assert resp.status_code == 201
    assert resp.json() == {"status": "recorded", "request_id": "req-ok"}

    # The Feedback row ties back to the TaskLog's provider/task_type.
    row = (await db_session.execute(select(Feedback))).scalars().one()
    assert row.request_id == "req-ok"
    assert row.provider_name == "groq"
    assert row.task_type == "summarization"
    assert row.rating == "positive"


async def test_feedback_unknown_request_id_returns_404(client):
    resp = await client.post(
        "/api/v1/feedback",
        json={"request_id": "req-ghost", "rating": "negative"},
    )
    assert resp.status_code == 404


async def test_feedback_on_failed_request_returns_400(client, db_session):
    _seed_log(db_session, "req-failed", status="failed", selected_model=None)
    await db_session.commit()

    resp = await client.post(
        "/api/v1/feedback",
        json={"request_id": "req-failed", "rating": "positive"},
    )
    assert resp.status_code == 400
    rows = (await db_session.execute(select(Feedback))).scalars().all()
    assert rows == []  # nothing persisted


async def test_feedback_duplicate_request_id_returns_409(client, db_session):
    _seed_log(db_session, "req-dup")
    await db_session.commit()

    first = await client.post(
        "/api/v1/feedback", json={"request_id": "req-dup", "rating": "positive"}
    )
    second = await client.post(
        "/api/v1/feedback", json={"request_id": "req-dup", "rating": "negative"}
    )
    assert first.status_code == 201
    assert second.status_code == 409

    # The original rating was NOT overwritten.
    rows = (await db_session.execute(select(Feedback))).scalars().all()
    assert len(rows) == 1
    assert rows[0].rating == "positive"


async def test_feedback_invalid_rating_returns_400(client, db_session):
    _seed_log(db_session, "req-invalid")
    await db_session.commit()

    resp = await client.post(
        "/api/v1/feedback", json={"request_id": "req-invalid", "rating": "meh"}
    )
    assert resp.status_code == 400


# --------------------------------------------------------------------------
# GET /api/v1/feedback/summary
# --------------------------------------------------------------------------


async def test_feedback_summary_empty_db(client):
    resp = await client.get("/api/v1/feedback/summary")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_rated"] == 0
    assert body["positive_rate"] == 0.0
    assert body["by_provider"] == {}
    assert body["by_task_type"] == {}


async def test_feedback_summary_aggregates_seeded_data(client, db_session):
    _seed_log(db_session, "r1", task_type="summarization", selected_model="groq")
    _seed_log(db_session, "r2", task_type="summarization", selected_model="groq")
    _seed_log(db_session, "r3", task_type="qa", selected_model="gemini")
    _seed_log(db_session, "r4", task_type="qa", selected_model="deepseek")
    await db_session.commit()

    db_session.add_all(
        [
            Feedback(request_id="r1", provider_name="groq", task_type="summarization", rating="positive"),
            Feedback(request_id="r2", provider_name="groq", task_type="summarization", rating="negative"),
            Feedback(request_id="r3", provider_name="gemini", task_type="qa", rating="positive"),
            Feedback(request_id="r4", provider_name="deepseek", task_type="qa", rating="negative"),
        ]
    )
    await db_session.commit()

    resp = await client.get("/api/v1/feedback/summary")
    assert resp.status_code == 200
    body = resp.json()

    assert body["total_rated"] == 4
    assert body["positive_rate"] == 0.5
    assert body["by_provider"]["groq"] == {
        "positive": 1, "negative": 1, "positive_rate": 0.5
    }
    assert body["by_provider"]["gemini"]["positive_rate"] == 1.0
    assert body["by_provider"]["deepseek"]["positive_rate"] == 0.0
    assert body["by_task_type"]["summarization"] == {
        "positive": 1, "negative": 1, "positive_rate": 0.5
    }
    assert body["by_task_type"]["qa"] == {
        "positive": 1, "negative": 1, "positive_rate": 0.5
    }


# --------------------------------------------------------------------------
# GET /api/v1/logs — feedback_given flag
# --------------------------------------------------------------------------


async def test_logs_include_feedback_given_flag(client, db_session):
    _seed_log(db_session, "r-rated")
    _seed_log(db_session, "r-unrated")
    await db_session.commit()
    await client.post(
        "/api/v1/feedback", json={"request_id": "r-rated", "rating": "positive"}
    )

    resp = await client.get("/api/v1/logs")
    by_id = {log["request_id"]: log for log in resp.json()["logs"]}
    assert by_id["r-rated"]["feedback_given"] is True
    assert by_id["r-unrated"]["feedback_given"] is False


# --------------------------------------------------------------------------
# Feedback reaches the routing decision (executor stats snapshot)
# --------------------------------------------------------------------------


async def test_stats_snapshot_picks_up_feedback_counts(db_session, breaker):
    """The bridge between Part A and Part B: _build_stats_snapshot aggregates
    the Feedback table into ModelStats so the scorer can blend it."""
    db_session.add_all(
        [
            Feedback(provider_name="groq", task_type="summarization", rating="positive", request_id="x1"),
            Feedback(provider_name="groq", task_type="summarization", rating="positive", request_id="x2"),
            Feedback(provider_name="groq", task_type="summarization", rating="negative", request_id="x3"),
            Feedback(provider_name="groq", task_type="qa", rating="positive", request_id="x4"),
            Feedback(provider_name="gemini", task_type="summarization", rating="negative", request_id="x5"),
        ]
    )
    await db_session.commit()

    snapshot = await _build_stats_snapshot(
        "summarization", circuit_breaker=breaker, db_session=db_session
    )
    groq = snapshot["groq"]
    assert groq.positive_feedback_count == 2
    assert groq.negative_feedback_count == 1
    # Other task types do NOT leak in:
    gemini = snapshot["gemini"]
    assert gemini.positive_feedback_count == 0
    assert gemini.negative_feedback_count == 1
    assert groq.feedback_adjusted_success_rate < groq.success_rate  # blend pulled down


# --------------------------------------------------------------------------
# End-to-end: successful routing → real TaskLog → 201 feedback
# --------------------------------------------------------------------------


async def test_feedback_ties_back_to_real_routed_request(client, install_stubs):
    install_stubs(
        groq=StubProvider("groq", [ok("groq", tokens=11)]),
        gemini=StubProvider("gemini", [ok("gemini")]),
        deepseek=StubProvider("deepseek", [ok("deepseek")]),
    )
    route = await client.post("/api/v1/route", json={"prompt": "hi", "task_type": "qa"})
    request_id = route.json()["request_id"]

    resp = await client.post(
        "/api/v1/feedback", json={"request_id": request_id, "rating": "negative"}
    )
    assert resp.status_code == 201
