"""Catalog endpoint: curated metadata merged with live provider telemetry."""

import httpx
import pytest

from app.core.model_catalog import MODEL_CATALOG
from app.core.scorer import COST_PER_1K_TOKENS
from app.main import app
from app.models import ModelStatsDB
from app.providers.registry import PROVIDER_REGISTRY


@pytest.fixture
async def client(monkeypatch, db_session, breaker):
    from app.api import catalog_routes
    from app.db.session import get_session

    async def override_session():
        yield db_session

    monkeypatch.setattr(
        app, "dependency_overrides", {get_session: override_session}
    )
    monkeypatch.setattr(catalog_routes, "_get_breaker", lambda: breaker)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_catalog_merges_static_and_live_data_for_all_providers(
    client, db_session, breaker
):
    db_session.add_all(
        [
            ModelStatsDB(
                provider_name="groq", task_type="qa", total_requests=100,
                successful_requests=94, avg_latency_ms=300.0, success_rate=0.94,
            ),
            ModelStatsDB(
                provider_name="groq", task_type="code", total_requests=100,
                successful_requests=80, avg_latency_ms=400.0, success_rate=0.80,
            ),
            ModelStatsDB(
                provider_name="deepseek", task_type="code", total_requests=10,
                successful_requests=5, avg_latency_ms=900.0, success_rate=0.5,
            ),
        ]
    )
    await db_session.commit()
    for _ in range(5):
        await breaker.record_failure("gemini")

    response = await client.get("/api/v1/catalog")
    assert response.status_code == 200
    data = response.json()
    assert [entry["provider_name"] for entry in data] == list(PROVIDER_REGISTRY)

    by_name = {entry["provider_name"]: entry for entry in data}
    for name, entry in by_name.items():
        metadata = MODEL_CATALOG[name]
        assert entry["display_name"] == metadata["display_name"]
        assert entry["description"] == metadata["description"]
        assert entry["context_window"] == metadata["context_window"]
        assert entry["capabilities"] == metadata["capabilities"]
        assert entry["pricing_tier"] == metadata["pricing_tier"]
        assert entry["cost_per_1k_tokens"] == COST_PER_1K_TOKENS[name]

    assert by_name["groq"]["live_success_rate"] == 0.87
    assert by_name["groq"]["avg_latency_ms"] == 350.0
    assert by_name["groq"]["current_status"] == "healthy"
    # No history uses the same optimistic default as the health board.
    assert by_name["gemini"]["live_success_rate"] == 0.75
    assert by_name["gemini"]["avg_latency_ms"] == 0.0
    assert by_name["gemini"]["current_status"] == "circuit_open"
    assert by_name["deepseek"]["current_status"] == "degraded"


async def test_catalog_is_public_and_empty_history_is_safe(client):
    response = await client.get("/api/v1/catalog")
    assert response.status_code == 200
    assert len(response.json()) == len(PROVIDER_REGISTRY)
    assert all(entry["current_status"] == "healthy" for entry in response.json())
