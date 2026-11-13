"""T3: walk policy and per-key memory, end to end (upstream stubbed).

Scenarios pinned against OBSERVED LIVE behavior from the owner's Codespace:
- a 404 (retired model) walks to the next candidate (max 3 attempts);
- a 403 PERMISSION_DENIED probes ONE different-family model, then stops and
  is never cached as model-denied;
- a 429 stops immediately (no walk) and parks the model for Retry-After;
- the replacement id from "use models/X" is extracted from the FULL body,
  verified against the discovered list AND the Auto allow-list;
- the last-known-working model is tried first;
- statuses persist to the DB with TTLs (denied 24h, rate_limited fallback).
"""

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.api.routes import limiter
from app.config import settings
from app.core import model_discovery as md
from app.main import app
from app.models import ProviderModelStatusDB
from app.providers.base import ModelProvider, make_error, make_success

GEMINI_SECRET = "AIzaSyDUMMY_walk_policy_secret_123"

#: OBSERVED LIVE, verbatim (truncated by the old 200-char cap in the app's
#: log — the message itself continued, which is exactly why T3/T4 raise the
#: cap to 1000).
RETIRED_MODEL_404_BODY = (
    "This model models/gemini-2.5-flash is no longer available to new users. "
    "Please update your code to use models/gemini-3.8-flash for the latest "
    "features and improve"
)


def _model(model_id: str, version: tuple) -> md.DiscoveredModel:
    return md.DiscoveredModel(
        id=model_id,
        display_name=model_id,
        version=version,
        created=None,
        context_window=None,
        max_output=None,
        chat_capable=True,
        capabilities=["generateContent"],
    )


DISCOVERED = [
    _model("gemini-2.5-flash", (2, 5)),
    _model("gemini-3.8-flash", (3, 8)),
    _model("gemini-2.5-pro", (2, 5)),
    _model("gemma-4-31b-it", (4,)),
]


class PolicyStub(ModelProvider):
    """Scripted per-model responses, recorded.

    Unscripted models answer 404 model_not_found (the realistic default for
    a key whose list shows the id but whose account cannot call it) — never
    None, which would fake an app bug instead of exercising policy.
    """

    provider_name = "gemini"
    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self, script: dict[str, object]):
        self.script = script
        self.calls: list[str] = []

    async def generate(self, prompt: str, **kwargs):
        model = kwargs.get("model", self.DEFAULT_MODEL)
        self.calls.append(model)
        outcome = self.script.get(model)
        if callable(outcome):
            return outcome(model)
        if outcome is None:
            return not_found(model)
        return outcome  # a ProviderResponse


def ok(model_id: str):
    return make_success("gemini", model_id, f"{model_id} says hi", 5, 12.0)


def not_found(model_id: str):
    return make_error(
        "gemini",
        model_id,
        f"model_not_found: gemini (generativelanguage.googleapis.com) has no "
        f"model named '{model_id}' for this key (HTTP 404). The URL and your "
        f"key are fine — the model string is wrong, retired, or not enabled "
        f"for your account. Provider said: {RETIRED_MODEL_404_BODY}",
        9.0,
        outcome="model_not_found",
    )


def permission_denied(model_id: str):
    return make_error(
        "gemini",
        model_id,
        f"auth_failed: gemini (generativelanguage.googleapis.com) rejected "
        f"the credential (HTTP 403).",
        8.0,
        outcome="auth_failed",
    )


def rate_limited(model_id: str, retry_after: float | None = None):
    return make_error(
        "gemini",
        model_id,
        "rate_limit_exceeded: gemini (generativelanguage.googleapis.com) is "
        "rate limiting this key (HTTP 429).",
        6.0,
        outcome="rate_limit_exceeded",
        retry_after=retry_after,
    )


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(
        settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(settings, "GROQ_API_KEY", None)
    monkeypatch.setattr(settings, "GEMINI_API_KEY", None)
    monkeypatch.setattr(settings, "DEEPSEEK_API_KEY", None)
    monkeypatch.setattr(settings, "GEMINI_MODEL", None)


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    yield
    limiter.reset()


@pytest.fixture(autouse=True)
def _fresh_caches():
    md.clear_caches()
    yield
    md.clear_caches()


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client):
    from app.db.session import get_session

    async def override_session():
        yield db_session

    monkeypatch.setattr(app, "dependency_overrides", {get_session: override_session})
    from app.core import executor as executor_module
    from app.core.chaos import ChaosController

    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


@pytest.fixture
def stub_gemini(monkeypatch):
    def _install(stub: PolicyStub):
        from app.providers.registry import PROVIDER_REGISTRY

        monkeypatch.setitem(PROVIDER_REGISTRY, "gemini", stub)
        return stub

    return _install


@pytest.fixture
def fake_discovery(monkeypatch):
    async def _discover(provider, api_key, force=False):
        assert api_key == GEMINI_SECRET
        return list(DISCOVERED), None

    monkeypatch.setattr(md, "discover_models", _discover)
    return _discover


async def _setup(client, monkeypatch, stub, monkeypatch_discovery=None):
    monkeypatch_discovery = monkeypatch_discovery or fake_discovery
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t3"})
        ).json()["api_key"]
    }
    save = await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    assert save.status_code == 200, save.text
    stubbed = stub(stub)
    if monkeypatch_discovery is not None:
        pass
    return headers, stubbed


# --------------------------------------------------------------------------
# 404 / replacement
# --------------------------------------------------------------------------


async def test_retired_404_walks_to_replacement_named_in_full_body(
    client, monkeypatch, stub_gemini, fake_discovery
):
    """OBSERVED LIVE observation 3: gemini-2.5-flash 404s, the body names
    models/gemini-3.8-flash. The walk must land there, in ONE request."""
    stub = PolicyStub({
        "gemini-2.5-flash": not_found("gemini-2.5-flash"),
        "gemini-3.8-flash": ok("gemini-3.8-flash"),
    })
    stub_gemini(stub)
    monkeypatch.setattr(md, "discover_models", fake_discovery)
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t3"})
        ).json()["api_key"]
    }
    await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    response = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    body = response.json()
    assert body["success"] is True
    # The walk saw the retired model, then the replacement — nothing else.
    assert stub.calls == ["gemini-2.5-flash", "gemini-3.8-flash"]
    chain = body["fallback_chain_attempted"]
    models = [a.get("model") for a in chain if a.get("model")]
    assert "gemini-2.5-flash" in models and "gemini-3.8-flash" in models
    # ...and the switched model is visible, never a silent switch.
    assert "gemini-2.5-flash" in response.text or any(
        a.get("error") for a in chain
    )


async def test_replacement_hint_to_an_excluded_id_is_ignored(
    client, monkeypatch, stub_gemini, fake_discovery
):
    """The provider suggests a TTS model — the allow-list must refuse it."""
    import re

    body_with_tts_hint = RETIRED_MODEL_404_BODY.replace(
        "models/gemini-3.8-flash", "models/gemini-3.8-flash-tts"
    )
    stub = PolicyStub({
        "gemini-2.5-flash": make_error(
            "gemini",
            "gemini-2.5-flash",
            f"model_not_found: Provider said: {body_with_tts_hint}",
            9.0,
            outcome="model_not_found",
        ),
        "gemini-3.8-flash": ok("gemini-3.8-flash"),
    })
    stub_gemini(stub)
    monkeypatch.setattr(md, "discover_models", fake_discovery)
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t3b"})
        ).json()["api_key"]
    }
    await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    response = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert response.json()["success"] is True
    # The TTS hint was never followed: the walk used rank order instead.
    assert stub.calls == ["gemini-2.5-flash", "gemini-3.8-flash"]


async def test_walk_is_capped_at_three_attempts_per_request(
    client, monkeypatch, stub_gemini, fake_discovery
):
    stub = PolicyStub({m: not_found(m) for m in [
        "gemini-2.5-flash", "gemini-3.8-flash", "gemini-2.5-pro", "gemma-4-31b-it",
    ]})
    stub_gemini(stub)
    monkeypatch.setattr(md, "discover_models", fake_discovery)
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t3c"})
        ).json()["api_key"]
    }
    await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    response = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert response.json()["success"] is False
    # Initial attempt + max_attempts-1 walk steps: three upstream calls total.
    assert len(stub.calls) == 3


# --------------------------------------------------------------------------
# 403: one cross-family probe, then stop; never cached as model-denied
# --------------------------------------------------------------------------


async def test_403_probes_one_other_family_then_stops(
    client, monkeypatch, stub_gemini, fake_discovery
):
    """OBSERVED LIVE observation 4 shape: PERMISSION_DENIED 403s."""
    stub = PolicyStub({
        "gemini-2.5-flash": permission_denied("gemini-2.5-flash"),
        "gemini-3.8-flash": permission_denied("gemini-3.8-flash"),
        "gemini-2.5-pro": permission_denied("gemini-2.5-pro"),
        "gemma-4-31b-it": ok("gemma-4-31b-it"),
    })
    stub_gemini(stub)
    monkeypatch.setattr(md, "discover_models", fake_discovery)
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t3d"})
        ).json()["api_key"]
    }
    await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    response = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    body = response.json()
    assert body["success"] is True
    # Initial gemini candidate 403s → ONE gemma probe succeeds. No second
    # gemini model was tried after the probe (all of gemini is key-wide).
    assert stub.calls == ["gemini-2.5-flash", "gemma-4-31b-it"]
    # A 403 is NEVER cached as model-denied for this key.
    assert md.get_cached_status(GEMINI_SECRET, "gemini", "gemini-2.5-flash") is None


async def test_403_with_no_other_family_stops_without_a_probe(
    client, monkeypatch, stub_gemini
):
    """Discovery exposes gemini models only: the 403 stops after ONE probe
    attempt is impossible — no different family exists, so it stops at once."""
    gemini_only = [m for m in DISCOVERED if not m.id.startswith("gemma")]

    async def _discover(provider, api_key, force=False):
        assert api_key == GEMINI_SECRET
        return list(gemini_only), None

    monkeypatch.setattr(md, "discover_models", _discover)
    stub = PolicyStub({
        "gemini-2.5-flash": permission_denied("gemini-2.5-flash"),
        "gemini-3.8-flash": permission_denied("gemini-3.8-flash"),
        "gemini-2.5-pro": permission_denied("gemini-2.5-pro"),
    })
    stub_gemini(stub)
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t3e"})
        ).json()["api_key"]
    }
    await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    response = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert response.json()["success"] is False
    # No different family exists in discovery: no probe, stop immediately.
    assert stub.calls == ["gemini-2.5-flash"]


# --------------------------------------------------------------------------
# 429: stop immediately, park until Retry-After
# --------------------------------------------------------------------------


async def test_429_stops_the_walk_and_parks_the_model_until_retry_after(
    client, monkeypatch, stub_gemini, fake_discovery
):
    stub = PolicyStub({
        "gemini-2.5-flash": rate_limited("gemini-2.5-flash", retry_after=45.0),
        "gemini-3.8-flash": ok("gemini-3.8-flash"),
    })
    stub_gemini(stub)
    monkeypatch.setattr(md, "discover_models", fake_discovery)
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t3f"})
        ).json()["api_key"]
    }
    await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    response = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert response.json()["success"] is False
    # 429 is NOT walkable: the initial attempt answers, no walk at all —
    # gemini-3.8-flash stays untouched.
    assert stub.calls == ["gemini-2.5-flash"]
    # Parked with the provider's Retry-After as the TTL.
    assert md.get_cached_status(GEMINI_SECRET, "gemini", "gemini-2.5-flash") == (
        "rate_limited"
    )


def test_rate_limited_ttl_honors_retry_after_and_denied_uses_24h():
    md.clear_caches()
    md.mark_status(GEMINI_SECRET, "gemini", "gemini-3.8-flash", "rate_limited", retry_after=120.0)
    entries = md._status_cache[(md.key_fingerprint(GEMINI_SECRET), "gemini")]
    ttl = entries["gemini-3.8-flash"][1] - md._now()
    assert 110 < ttl <= 120
    md.clear_caches()
    md.mark_status(GEMINI_SECRET, "gemini", "gemini-2.5-flash", "denied")
    entries = md._status_cache[(md.key_fingerprint(GEMINI_SECRET), "gemini")]
    ttl = entries["gemini-2.5-flash"][1] - md._now()
    assert 86000 < ttl <= 86400


# --------------------------------------------------------------------------
# Last-known-good + DB persistence
# --------------------------------------------------------------------------


async def test_last_known_good_model_is_tried_first(
    client, monkeypatch, stub_gemini, fake_discovery
):
    stub = PolicyStub({"gemini-3.8-flash": ok("gemini-3.8-flash")})
    stub_gemini(stub)
    monkeypatch.setattr(md, "discover_models", fake_discovery)
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t3g"})
        ).json()["api_key"]
    }
    await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    first = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert first.json()["success"] is True
    second = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert second.json()["success"] is True
    # First request: legacy initial (provider default 2.5-flash) 404s → the
    # walk lands on 3.8 and remembers it. Second request: the remembered
    # last-known-good is tried FIRST — one upstream call, no detour.
    assert stub.calls == [
        "gemini-2.5-flash",
        "gemini-3.8-flash",
        "gemini-3.8-flash",
    ]


async def test_statuses_persist_to_the_db_for_restart(
    client, monkeypatch, stub_gemini, fake_discovery, db_session
):
    stub = PolicyStub({
        "gemini-3.8-flash": not_found("gemini-3.8-flash"),
        "gemini-2.5-pro": ok("gemini-2.5-pro"),
    })
    stub_gemini(stub)
    monkeypatch.setattr(md, "discover_models", fake_discovery)
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t3h"})
        ).json()["api_key"]
    }
    await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    # Walk budget (3 total): initial 2.5-flash 404 → 3.8-flash 404 → the
    # next candidate 2.5-pro answers.
    response = await client.post("/api/v1/route", json={"prompt": "hi"}, headers=headers)
    assert response.json()["success"] is True
    rows = (
        await db_session.execute(select(ProviderModelStatusDB))
    ).scalars().all()
    statuses = {row.model_id: row.status for row in rows}
    assert statuses.get("gemini-2.5-flash") == "denied"
    assert statuses.get("gemini-3.8-flash") == "denied"
    assert statuses.get("gemini-2.5-pro") == "works"


async def test_db_denied_reader_enforces_the_24h_ttl(monkeypatch):
    """A 'denied' row older than 24h must NOT be treated as still denied."""
    from datetime import datetime, timedelta, timezone

    from app.models.provider_model_status import ProviderModelStatusDB

    now = datetime.now(timezone.utc)
    rows = {
        "gemini-2.5-flash": ProviderModelStatusDB(
            api_key_id=1,
            provider_name="gemini",
            model_id="gemini-2.5-flash",
            status="denied",
            last_checked_at=now - timedelta(hours=1),
        ),
        "gemini-3.8-flash": ProviderModelStatusDB(
            api_key_id=1,
            provider_name="gemini",
            model_id="gemini-3.8-flash",
            status="denied",
            last_checked_at=now - timedelta(hours=25),
        ),
        "gemini-2.5-pro": ProviderModelStatusDB(
            api_key_id=1,
            provider_name="gemini",
            model_id="gemini-2.5-pro",
            status="rate_limited",
            last_checked_at=now - timedelta(seconds=30),
        ),
        "gemma-4-31b-it": ProviderModelStatusDB(
            api_key_id=1,
            provider_name="gemini",
            model_id="gemma-4-31b-it",
            status="works",
            last_checked_at=now - timedelta(minutes=2),
        ),
    }

    async def fake_load_statuses(_db, _id, _provider):
        return rows

    monkeypatch.setattr(md, "load_db_statuses", fake_load_statuses)
    denied = await md.db_denied_models(None, 1, "gemini")
    # Fresh denied counts; the 25h-old denial expired; rate_limited within
    # its fallback TTL counts; "works" is never a denied id.
    assert denied == {"gemini-2.5-flash", "gemini-2.5-pro"}
    last_good = await md.db_last_known_good(None, 1, "gemini")
    assert last_good == "gemma-4-31b-it"
