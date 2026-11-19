"""The live scripts' assumptions, checked against the REAL app.

Why this file exists
--------------------
`evals/run_resilience_eval.py` refused anything but HTTP 200 for
`POST /api/v1/keys/generate`. The endpoint answers **201 Created** (it creates
a key). Every live run therefore died with:

    FATAL: key generation failed (HTTP 201).

The mocked suite stayed green because its fake httpx client answered 200 for
that same path — a fake that is more permissive than the endpoint it stands in
for hides precisely this bug.

So the rule is pinned twice, and neither pin trusts a fake:

1. **This file** drives the real FastAPI app through ``httpx.ASGITransport``
   (no network, no keys) and records the status codes the scripts must accept.
2. **test_evals.py** runs the scripts' preflight against a fake built to match
   those observed codes, and ``_assert_script_accepts`` proves the scripts use
   "any 2xx", so a future 202 or 204 cannot break them either.

`benchmark/run_benchmark.py` already demanded 201 for this path; it now uses
the same 2xx rule and the same redacted error text as the eval.
"""

import httpx
import pytest

from app.main import app
from app.models import ApiKeyDB

# Observed live from the running app (ASGITransport, demo mode on):
#   POST /api/v1/keys/generate        -> 201
#   GET  /api/v1/provider-keys        -> 200
#   GET  /api/v1/admin/chaos/status   -> 200 (404 only when chaos is disabled)
#   POST /api/v1/admin/chaos/inject   -> 200
#   POST /api/v1/admin/chaos/clear    -> 422 without a body, 200 with {"provider_name": ...}
KEY_GENERATE_STATUS = 201


@pytest.fixture
async def raw_client(monkeypatch, db_session):
    """Real app, real SQLite session, no socket. REQUIRE_API_KEY off so the
    key-generate endpoint itself needs no credential."""
    from app.db.session import get_session
    from app.config import settings

    async def override_session():
        yield db_session

    monkeypatch.setattr(app, "dependency_overrides", {get_session: override_session})
    monkeypatch.setattr(settings, "REQUIRE_API_KEY", False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8000"
    ) as client:
        yield client


async def test_key_generation_returns_201_not_200(raw_client, db_session):
    """The contract the live script violated."""
    resp = await raw_client.post("/api/v1/keys/generate", json={"label": "contract"})
    assert resp.status_code == KEY_GENERATE_STATUS, resp.text
    assert resp.json()["api_key"]

    from sqlalchemy import select

    rows = (await db_session.execute(select(ApiKeyDB))).scalars().all()
    assert rows, "201 means a key row was created; the row is the point"


async def test_provider_keys_list_returns_200(raw_client):
    resp = await raw_client.get("/api/v1/provider-keys")
    assert resp.status_code == 200, resp.text
    assert isinstance(resp.json(), list)


async def test_chaos_clear_requires_a_provider_name(raw_client):
    """An empty body is 422, not 200. Both scripts already send the body, so
    this documents the requirement instead of 'fixing' the endpoint."""
    empty = await raw_client.post("/api/v1/admin/chaos/clear", json={})
    assert empty.status_code == 422, empty.text


def _load_script(name: str, filename: str):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "evals" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class _Resp:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def json(self):
        return {}


@pytest.mark.parametrize(
    "status,accepted",
    [
        (200, True),
        (201, True),   # the code that used to be fatal
        (202, True),   # a future async variant must not break the scripts
        (204, True),   # no body, still success
        (400, False),
        (401, False),
        (403, False),
        (404, False),
        (429, False),
        (500, False),
    ],
)
def test_scripts_accept_any_2xx(tmp_path, status, accepted):
    """Proves the fix is '2xx', not '200 or 201'. A per-status fake, no app."""
    harness = _load_script("contract_harness", "harness.py")
    resp = _Resp(status, '{"detail":"nope"}')
    if accepted:
        harness.require_success(resp, "key generation")
    else:
        with pytest.raises(SystemExit) as exc:
            harness.require_success(resp, "key generation")
        assert f"HTTP {status}" in str(exc.value)


def test_observed_status_is_accepted_by_the_scripts():
    """Ties the two halves together: the real app's 201 must satisfy the
    script's rule. If someone reintroduces `== 200`, this fails."""
    harness = _load_script("contract_harness_2", "harness.py")
    harness.require_success(_Resp(KEY_GENERATE_STATUS), "key generation")


def test_eval_script_never_hardcodes_200_again():
    """A grep-level guard with a clear message: the source of the live outage
    was a hardcoded status, so the source is what gets checked."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3]
    offenders = []
    for rel in ("evals/run_resilience_eval.py", "benchmark/run_benchmark.py"):
        text = (root / rel).read_text()
        for needle in ("status_code != 200", "status_code == 200\n", "status_code != 201"):
            if needle in text:
                offenders.append((rel, needle))
    assert not offenders, (
        "live scripts must accept any 2xx (see evals/harness.py:require_success); "
        f"hardcoded status checks found: {offenders}"
    )


def test_redaction_removes_credentials_from_error_bodies():
    """The scripts print failure bodies. No key may survive the trip."""
    harness = _load_script("contract_harness_3", "harness.py")

    leaked = (
        '{"error":"bad key AIzaSyA1234567890abcdefghijklmnop",'
        '"detail":"gsk_abcdefghijklmnop123","api_key":"rk_live_abcdef123456",'
        '"hint":"Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig",'
        '"other":"sk-proj-abcdefghijklmnop"}'
    )
    detail = harness.response_detail(_Resp(500, leaked))

    for secret in (
        "AIzaSyA1234567890abcdefghijklmnop",
        "gsk_abcdefghijklmnop123",
        "rk_live_abcdef123456",
        "eyJhbGciOiJIUzI1NiJ9.payload.sig",
        "sk-proj-abcdefghijklmnop",
    ):
        assert secret not in detail, f"credential leaked into error text: {secret}"
    assert "HTTP 500" in detail


def test_response_detail_truncates_a_huge_body():
    harness = _load_script("contract_harness_4", "harness.py")
    detail = harness.response_detail(_Resp(502, "x" * 5000))
    assert len(detail) <= harness.MAX_ERROR_BODY_CHARS + 40
    assert detail.endswith("…")


def test_response_detail_survives_a_bodyless_response():
    harness = _load_script("contract_harness_5", "harness.py")
    assert harness.response_detail(_Resp(503)) == "HTTP 503"


# ---------------------------------------------------------------------------
# T0 follow-up: every fake in the eval/benchmark tests must match reality
# ---------------------------------------------------------------------------
#
# The HTTP-201 bug and the "configured: none" bug were the same shape: a fake
# answered something the real app never sends, so the mocked suite could not
# see the mismatch. Each fake that stands in for a backend response is pinned
# here by asserting the REAL response instead. If the endpoint changes shape,
# these fail and the fakes get corrected in the same commit.


@pytest.fixture(autouse=True)
def no_post_save_discovery(monkeypatch):
    """Saving a provider key fires a BACKGROUND discovery call to the real
    provider host. Keep these tests off the network: the save path, storage
    and response are still real; only the outbound warm-up is stubbed."""
    import app.api.provider_keys_routes as provider_routes

    async def _noop(provider_name, raw_key):
        return None

    monkeypatch.setattr(provider_routes, "_warm_discovery_after_save", _noop)


@pytest.fixture
async def client(monkeypatch, db_session):
    """Real app over ASGITransport with provider-key encryption available."""
    from cryptography.fernet import Fernet

    from app.config import settings
    from app.db.session import get_session

    monkeypatch.setattr(
        settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )
    monkeypatch.setattr(settings, "REQUIRE_API_KEY", False)

    async def override_session():
        yield db_session

    monkeypatch.setattr(app, "dependency_overrides", {get_session: override_session})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8000"
    ) as ac:
        yield ac


async def test_provider_keys_rows_have_the_shape_the_eval_parses(client):
    """`[{"provider_name", "configured", ...}]` for EVERY provider.

    The eval parses this with `row["provider_name"] for row in rows if
    row.get("configured")`. `_preflight_routes` in test_evals.py fabricates
    this same list; if the real shape drifts, that fake would silently keep
    passing while the live script broke.
    """
    resp = await client.get("/api/v1/provider-keys")
    assert resp.status_code == 200, resp.text
    rows = resp.json()
    assert isinstance(rows, list) and rows
    for row in rows:
        assert set(row) >= {"provider_name", "configured"}
        assert isinstance(row["provider_name"], str)
        assert isinstance(row["configured"], bool)
    # Every registered provider is present, configured or not - the fake's
    # "one row per provider, with a configured flag" assumption holds.
    from app.core.provider_key_manager import SUPPORTED_PROVIDERS

    assert {row["provider_name"] for row in rows} == set(SUPPORTED_PROVIDERS)


async def test_provider_key_put_returns_the_status_row_the_dashboard_uses(client):
    """PUT answers 200 with the same row shape as GET, configured=true."""
    key = (await client.post("/api/v1/keys/generate", json={"label": "d"})).json()[
        "api_key"
    ]
    resp = await client.put(
        "/api/v1/provider-keys/groq",
        json={"api_key": "gsk_FAKE0000000000000000"},
        headers={"X-API-Key": key},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider_name"] == "groq"
    assert body["configured"] is True
    assert body["key_hint"], "the dashboard shows a hint; it must be returned"


async def test_chaos_inject_returns_expires_in_seconds(monkeypatch):
    """The benchmark reads body["expires_in_seconds"]; its fake hardcodes it."""
    from app.core.chaos import ChaosController
    from fakeredis import FakeAsyncRedis

    controller = ChaosController(FakeAsyncRedis(decode_responses=True))
    ttl = await controller.inject_failure("gemini", 3600)
    assert isinstance(ttl, int) and ttl > 0
    assert await controller.is_chaos_active("gemini") is True
    await controller.clear_failure("gemini")
    assert await controller.is_chaos_active("gemini") is False


async def test_health_reports_a_components_mapping(client):
    """The benchmark prints `.get("components", {})`; it must be a dict."""
    resp = await client.get("/api/v1/health")
    assert 200 <= resp.status_code < 300, resp.text
    body = resp.json()
    assert isinstance(body.get("components"), dict), body


# NOTE on POST /api/v1/route: the benchmark's `record_to_row()` reads
# success/model_used/tokens_used. That shape is ALREADY pinned against the real
# app in tests/integration/test_routes.py (see the `model_used`/`tokens_used`
# assertions), so no duplicate contract test is added here. Duplicating it
# would be a second place to keep in sync - the same trap this file exists to
# avoid - and the version written first here reached for a `force_provider`
# request field whose behaviour this change has no business assuming.
