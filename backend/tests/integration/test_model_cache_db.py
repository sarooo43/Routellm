"""Database-backed half of the discovery cache.

Split from ``test_model_discovery.py`` because these need a real session (the
persistence layer goes through ``get_session_maker``, which unit tests do not
otherwise touch).

What matters: a restart must not re-ask every provider, and the table must
never become a credential store.
"""

import httpx
import pytest

from app.providers.model_registry import ModelCatalog, PROVIDER_SPECS

GROQ_MODELS = {
    "data": [
        {"id": "whisper-large-v3"},
        {"id": "llama-3.1-8b-instant"},
    ]
}


class _Ctx:
    """Async context manager over the test session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *_exc):
        return False


@pytest.fixture
def db_backed(monkeypatch, db_session):
    """Point the catalog's persistence at the per-test session."""

    class _Maker:
        def __call__(self):
            return _Ctx(db_session)

    monkeypatch.setattr(ModelCatalog, "session_maker", staticmethod(lambda: _Maker()))
    return db_session


# --------------------------------------------------------------------------
# Database persistence (survives a restart)
# --------------------------------------------------------------------------


async def test_discovery_is_persisted_and_reused_after_a_cold_start(respx_mock, db_backed):
    """A restart must not put a live list-models call on the first prompt."""
    from sqlalchemy import select

    from app.models.model_cache import DiscoveredModelCacheDB

    route = respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )

    first = ModelCatalog()
    models, source = await first.list_models(PROVIDER_SPECS["groq"], "gsk_persist_1234567890")
    assert source == "discovered"
    assert route.call_count == 1

    rows = (await db_backed.execute(select(DiscoveredModelCacheDB))).scalars().all()
    assert len(rows) == 1
    assert rows[0].provider == "groq"

    # A brand-new catalog is what a restarted process looks like.
    second = ModelCatalog()
    reused, source2 = await second.list_models(PROVIDER_SPECS["groq"], "gsk_persist_1234567890")
    assert reused == models
    assert source2 == "db_cache"
    assert route.call_count == 1  # still one provider call in total


async def test_persisted_rows_expire_like_the_memory_cache(respx_mock, db_backed):
    from sqlalchemy import select

    from app.models.model_cache import DiscoveredModelCacheDB

    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    await ModelCatalog(ttl_s=0.01).list_models(PROVIDER_SPECS["groq"], "gsk_stale_1234567890")
    row = (await db_backed.execute(select(DiscoveredModelCacheDB))).scalars().one()
    row.fetched_at = 0.0  # very old
    db_backed.add(row)
    await db_backed.commit()

    fresh = ModelCatalog()
    _models, source = await fresh.list_models(PROVIDER_SPECS["groq"], "gsk_stale_1234567890")
    assert source == "discovered"


async def test_the_database_stores_only_a_fingerprint(respx_mock, db_backed):
    from sqlalchemy import select

    from app.models.model_cache import DiscoveredModelCacheDB

    secret = "gsk_NEVERSTOREAPLAINKEY9999"
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    await ModelCatalog().list_models(PROVIDER_SPECS["groq"], secret)
    row = (await db_backed.execute(select(DiscoveredModelCacheDB))).scalars().one()
    dumped = f"{row.provider}{row.key_fingerprint}{row.models_json}"
    assert secret not in dumped
    assert len(row.key_fingerprint) == 16


async def test_a_missing_database_does_not_break_discovery(respx_mock, monkeypatch):
    """Local-first: no DB configured must still route."""

    def _boom(*_a, **_k):
        raise RuntimeError("no database here")

    monkeypatch.setattr(ModelCatalog, "session_maker", staticmethod(_boom))
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )

    models, source = await ModelCatalog().list_models(
        PROVIDER_SPECS["groq"], "gsk_nodb_1234567890"
    )
    assert source == "discovered"
    assert models


async def test_invalidate_everywhere_clears_both_tiers(respx_mock, db_backed):
    """After a 404 the next call must really re-ask, not replay the DB row."""
    from sqlalchemy import select

    from app.models.model_cache import DiscoveredModelCacheDB

    route = respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json=GROQ_MODELS)
    )
    catalog = ModelCatalog()
    await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_inval_1234567890")
    assert route.call_count == 1

    await catalog.invalidate_everywhere("groq")

    rows = (await db_backed.execute(select(DiscoveredModelCacheDB))).scalars().all()
    assert rows == []

    _models, source = await catalog.list_models(PROVIDER_SPECS["groq"], "gsk_inval_1234567890")
    assert source == "discovered"
    assert route.call_count == 2
