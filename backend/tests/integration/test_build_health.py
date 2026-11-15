"""T1 stale-build detection: /health carries the backend's build identity.

The dashboard's banner compares the bundle's frozen commit against this
payload, so the wire shape is the contract: ``build.git_commit`` present,
``None`` (never a crash) when git is unavailable, and never any
credential-shaped value in the block.
"""

import httpx
import pytest

from app.build_info import build_info, git_commit
from app.main import app


@pytest.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def test_health_reports_the_backend_build_commit(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["build"] == build_info()
    assert resp.json()["build"]["git_commit"] == git_commit()


async def test_the_api_health_alias_carries_the_same_build_block(client):
    """Both /health routes share health_payload — verify the alias too."""
    resp = await client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json()["build"] == build_info()


async def test_the_build_block_names_nothing_credential_shaped(client):
    body = (await client.get("/health")).json()["build"]
    serialized = repr(body)
    for marker in ("gsk_", "AIza", "sk-", "rk_", "postgres://", "rediss://"):
        assert marker not in serialized
