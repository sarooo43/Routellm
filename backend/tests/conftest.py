"""Suite-wide isolation for anything that would otherwise be stateful.

The model-discovery cache now has two tiers: memory, and a table in the
database. Tests run against whatever database the environment points at, and
under a plain ``pytest`` that is the developer's local SQLite file — the same
one ``python run.py`` uses. Without this fixture a unit test that discovers a
model list would write a row, and the next test in a *different* file would see
"db_cache" where it expects a live fetch.

So: every test starts with an empty memory cache and no persisted rows. Cheap,
and it keeps "the test pass did not touch my real data".
"""

import asyncio

import pytest


@pytest.fixture(autouse=True)
def _clear_model_discovery_state():
    from app.providers.model_registry import model_catalog

    model_catalog.clear()
    _purge_persisted_rows()
    yield
    model_catalog.clear()
    _purge_persisted_rows()


def _purge_persisted_rows() -> None:
    """Best-effort: delete persisted discovery rows, ignoring any failure.

    Runs its own event loop because it is autouse for sync *and* async tests,
    and it must never turn a database hiccup into a test failure.
    """
    try:
        asyncio.run(_purge_async())
    except Exception:  # noqa: BLE001 - isolation is best effort
        pass


async def _purge_async() -> None:
    from sqlalchemy import delete

    from app.db.session import get_session_maker
    from app.models.model_cache import DiscoveredModelCacheDB

    maker = get_session_maker()
    async with maker() as session:
        await session.execute(delete(DiscoveredModelCacheDB))
        await session.commit()
