"""T6: a key save must never fire POST /api/v1/route.

OBSERVED LIVE (owner's Codespace, main at 136fae0): a POST /api/v1/route with
task_type=code appeared about 1 second after a key was saved. Investigation of
main's code shows NO automatic sender exists:

- frontend: only three surfaces call postRoute, all user-clicked —
  TryItLive (RoutingExplorer page, Send button / Enter),
  DemoModeCard ("Send a demo prompt", and the checklist's "Try Demo mode"
  only OPENS the card), and nothing else;
- frontend never sends task_type at all — the `code` in the log is the
  backend classifier's RESOLVED value (executor.py:
  ``resolved_task_type = task_type if task_type else classify_task(prompt)``);
  TryItLive's example prompt "Write a Python function to merge two sorted
  lists" contains the code keywords "python"/"function" → classified code.
  So the observed request matches a user-clicked TryItLive send ~1 s after
  the save (human timing), not an automated one;
- main's PUT /provider-keys handler does no background work whatsoever.

These tests pin the invariant anyway: saving a key (and the explorer panel it
opens) issues discovery/list/probe traffic only — never a routing call. A
real /route call is observable as a TaskLog row, so an empty TaskLog table
after the save+list+model-test sequence is the behavior-level proof.
"""

import asyncio

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.api.routes import limiter
from app.config import settings
from app.core import executor as executor_module
from app.core.chaos import ChaosController
from app.main import app
from app.models import TaskLog
from tests.integration.conftest import StubProvider, ok


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(
        settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode("utf-8")
    )


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    yield
    limiter.reset()


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client, install_stubs):
    from app.db.session import get_session

    async def override_session():
        yield db_session

    monkeypatch.setattr(app, "dependency_overrides", {get_session: override_session})
    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )
    install_stubs(
        gemini=StubProvider("gemini", [ok("gemini", text="gemini ok", tokens=7)] * 10),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _task_log_count(db_session) -> int:
    return len((await db_session.execute(select(TaskLog))).scalars().all())


async def test_saving_a_key_issues_no_route_call(client, db_session):
    """PUT /provider-keys + the explorer panel it opens: no routing, ever."""
    headers = {"X-API-Key": (await client.post("/api/v1/keys/generate", json={"label": "t6"})).json()["api_key"]}

    save = await client.put(
        "/api/v1/provider-keys/gemini", json={"api_key": "AIzaFAKEKEY000000"}, headers=headers
    )
    assert save.status_code == 200, save.text

    # What the frontend does right after a save: open the panel, which loads
    # the discovered list and may run a model probe — never /route.
    listing = await client.get("/api/v1/provider-keys/gemini/models", headers=headers)
    assert listing.status_code == 200, listing.text
    probe = await client.post(
        "/api/v1/provider-test/gemini", headers=headers
    )
    assert probe.status_code in (200, 503), probe.text

    # The warm post-save discovery runs as a background task; give it a beat.
    await asyncio.sleep(0.05)

    assert await _task_log_count(db_session) == 0


async def test_route_call_creates_a_task_log_row_so_the_distinction_is_observable(
    client, db_session
):
    """Control: a REAL /route call DOES write a TaskLog — the test above is
    meaningful because this one is not empty."""
    headers = {"X-API-Key": (await client.post("/api/v1/keys/generate", json={"label": "t6b"})).json()["api_key"]}
    response = await client.post(
        "/api/v1/route",
        json={"prompt": "Write a Python function to merge two sorted lists"},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert await _task_log_count(db_session) == 1
