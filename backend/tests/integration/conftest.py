"""Shared integration-test plumbing.

Uses SQLite in-memory (aiosqlite + StaticPool) as the test database — fast
and dependency-free. Note: SQLAlchemy's JSON column type serializes the
same on SQLite and Postgres, but SQLite stores the value as TEXT; we only
round-trip fallback_chain (never query/filter on it), which keeps behavior
identical across both dialects.
"""

import pytest
from fakeredis import FakeAsyncRedis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import app.models  # noqa: F401  (register all tables on SQLModel.metadata)
import app.providers.registry  as registry_module
from app.core.api_key_manager import generate_api_key
from app.core.circuit_breaker import CircuitBreaker
from app.core.quota_tracker import QuotaTracker
from app.core.scorer import rank_models
from app.models.api_key import ApiKeyDB
from app.providers.base import ModelProvider, ProviderResponse
from app.providers.registry import PROVIDER_REGISTRY

# With zero-history stats, default weights rank free+fast providers above
# deepseek. To place a gate-blocked provider FIRST (so quota gating is
# exercised before healthier providers), we use capability-dominant weights —
# the scorer's documented tunability. (Circuit-OPEN providers ALWAYS rank
# last regardless of weights — that scorer guarantee is not overridable.)
CAPABILITY_ONLY_WEIGHTS = {
    "capability": 1.0,
    "success_rate": 0.0,
    "cost": 0.0,
    "latency": 0.0,
    "availability": 0.0,
}


class StubProvider(ModelProvider):
    def __init__(self, provider_name: str, responses: list[ProviderResponse]):
        self.provider_name = provider_name
        self.responses = list(responses)
        self.calls = 0
        self.prompts_seen: list[str] = []
        self.kwargs_seen: list[dict] = []

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        self.calls += 1
        self.prompts_seen.append(prompt)
        self.kwargs_seen.append(kwargs)
        if self.responses:
            return self.responses.pop(0)
        return ProviderResponse(
            success=False,
            error="no_stub_response",
            provider_name=self.provider_name,
            model_name="stub-model",
        )


class RecordingBreaker(CircuitBreaker):
    """Delegating spy: count record_success/record_failure invocations."""

    def __init__(self, redis_client):
        super().__init__(redis_client)
        self.success_calls: list[str] = []
        self.failure_calls: list[str] = []

    async def record_success(self, provider_name):
        self.success_calls.append(provider_name)
        await super().record_success(provider_name)

    async def record_failure(self, provider_name):
        self.failure_calls.append(provider_name)
        await super().record_failure(provider_name)


def ok(
    provider_name: str,
    text: str | None = None,
    tokens: int = 11,
    latency: float = 50.0,
) -> ProviderResponse:
    return ProviderResponse(
        success=True,
        text=text if text is not None else f"{provider_name} says hi",
        tokens_used=tokens,
        latency_ms=latency,
        provider_name=provider_name,
        model_name="stub-model",
    )


def fail(
    provider_name: str,
    error: str = "rate_limit_exceeded",
    latency: float = 25.0,
    tokens: int = 0,
) -> ProviderResponse:
    return ProviderResponse(
        success=False,
        error=error,
        latency_ms=latency,
        tokens_used=tokens,
        provider_name=provider_name,
        model_name="stub-model",
    )


def expected_ranking(task_type: str, weights: dict | None = None) -> list[str]:
    ranked = rank_models(task_type, list(PROVIDER_REGISTRY.keys()), {}, weights)
    return [r.provider_name for r in ranked]


@pytest.fixture
async def redis_client():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.flushall()


@pytest.fixture
def breaker(redis_client):
    return RecordingBreaker(redis_client)


@pytest.fixture
def tracker(redis_client):
    return QuotaTracker(redis_client)


@pytest.fixture
async def db_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    maker = async_sessionmaker(db_engine, expire_on_commit=False)
    async with maker() as session:
        yield session


@pytest.fixture
async def auth_headers(db_session):
    """A real generated key for tests that exercise protected core endpoints."""
    raw_key, key_hash, key_prefix = generate_api_key()
    db_session.add(ApiKeyDB(key_hash=key_hash, key_prefix=key_prefix, label="Test key"))
    await db_session.commit()
    return {"X-API-Key": raw_key}


@pytest.fixture
def install_stubs(monkeypatch):
    """Replace REAL providers in the registry with stubs (auto-restored)."""

    def _install(**providers: StubProvider):
        for name, stub in providers.items():
            monkeypatch.setitem(PROVIDER_REGISTRY, name, stub)
        return providers

    return _install


# Keep the real registry import alive so it is never tree-shaken away by
# linters (used implicitly via install_stubs).
assert registry_module.PROVIDER_REGISTRY is PROVIDER_REGISTRY
