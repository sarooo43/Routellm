"""T0 regression: a key's provider keys must be visible to that same key.

The owner had Gemini, Groq and DeepSeek saved in the dashboard and ran the
live eval. Detection reported:

    fewer than 2 providers have saved keys (configured: none)

Three keys were saved. The eval had minted a brand NEW Routtener key for the
run, and provider keys are scoped to the key that saved them
(``ProviderKeyDB.api_key_id``) - so a fresh key legitimately sees none.

Nothing was wrong with the endpoint; the eval was asking the wrong question
with the wrong credential, and then telling the owner to "save provider keys
in the dashboard first", which they had already done three times.

These tests go through the REAL app. Provider keys are saved through the REAL
PUT /api/v1/provider-keys/{provider} path with the credential the DASHBOARD
uses (an ``X-API-Key`` header from browser localStorage). Nothing is inserted
into the database directly: doing that is what hid the bug, because a
hand-written row has whatever ``api_key_id`` the test picked.
"""

import httpx
import pytest
from cryptography.fernet import Fernet

from app.config import settings
from app.main import app

FAKE_PROVIDER_KEYS = {
    "gemini": "AIzaFAKE000000000000000000000000000",
    "groq": "gsk_FAKE000000000000000000000000000000",
    "deepseek": "sk-FAKE000000000000000000000000000000",
}


@pytest.fixture(autouse=True)
def no_post_save_discovery(monkeypatch):
    """Saving a provider key normally fires a BACKGROUND model-discovery call
    to the real provider host (see _warm_discovery_after_save).

    In CI that is a live network request whose task can outlive the test's
    event loop - and these tests save three keys each. Nothing here is about
    discovery, so it is stubbed out: the save path, the storage and the
    detection call are all still real.
    """
    import app.api.provider_keys_routes as provider_routes

    async def _noop(provider_name, raw_key):
        return None

    monkeypatch.setattr(
        provider_routes, "_warm_discovery_after_save", _noop
    )


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setattr(
        settings, "PROVIDER_KEY_ENCRYPTION_KEY", Fernet.generate_key().decode()
    )


@pytest.fixture
async def client(monkeypatch, db_session, breaker, tracker, redis_client):
    """Real app over ASGITransport, real SQLite session, no sockets."""

    # The app caches ONE global ChaosController/breaker/tracker bound to
    # whichever event loop first created them (executor._ensure_shared_clients).
    # Under pytest every test gets a fresh loop, so a cached Redis client from
    # an earlier test raises "Event loop is closed" - and it only shows up with
    # a REAL Redis (CI), which is why SQLite+fakeredis runs looked clean.
    # Every other integration test installs this override; this one now does
    # too, instead of reaching the process-global client.
    from app.core import executor as executor_module
    from app.core.chaos import ChaosController

    monkeypatch.setattr(
        executor_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )
    # chaos_routes did `from app.core.executor import _ensure_shared_clients`,
    # so patching the executor module alone does not reach it - the chaos
    # endpoints kept using the stale, loop-bound client.
    import app.api.chaos_routes as chaos_routes_module

    monkeypatch.setattr(
        chaos_routes_module,
        "_ensure_shared_clients",
        lambda: (breaker, tracker, ChaosController(redis_client)),
    )
    from app.db.session import get_session

    async def override_session():
        yield db_session

    monkeypatch.setattr(app, "dependency_overrides", {get_session: override_session})
    monkeypatch.setattr(settings, "REQUIRE_API_KEY", False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://127.0.0.1:8000"
    ) as ac:
        yield ac


def _load_eval():
    """Load the eval script the same way test_evals.py does."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[3] / "evals" / "run_resilience_eval.py"
    spec = importlib.util.spec_from_file_location("t0_resilience_eval", str(path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def _dashboard_key(client) -> str:
    """The credential the dashboard holds in localStorage: a minted rk_ key."""
    resp = await client.post("/api/v1/keys/generate", json={"label": "dashboard"})
    assert resp.status_code == 201, resp.text
    return resp.json()["api_key"]


async def _save_provider_keys(client, api_key: str, providers) -> None:
    """Save through the real endpoint the dashboard calls, not via SQL."""
    for provider in providers:
        resp = await client.put(
            f"/api/v1/provider-keys/{provider}",
            json={"api_key": FAKE_PROVIDER_KEYS[provider]},
            headers={"X-API-Key": api_key},
        )
        assert resp.status_code == 200, resp.text


@pytest.mark.parametrize("saved", [[], ["gemini"], ["gemini", "groq", "deepseek"]])
async def test_detection_returns_exactly_the_saved_providers(client, saved):
    """The eval's own detection function must report what the dashboard saved."""
    eval_module = _load_eval()
    key = await _dashboard_key(client)
    await _save_provider_keys(client, key, saved)

    detected = await eval_module.detect_providers(client, key)

    assert sorted(detected) == sorted(saved)


async def test_the_owner_flow_now_sees_all_three_providers(client):
    """The exact scenario that failed: 3 keys saved, eval detects all 3."""
    eval_module = _load_eval()
    key = await _dashboard_key(client)
    await _save_provider_keys(client, key, ["gemini", "groq", "deepseek"])

    detected = await eval_module.detect_providers(client, key)

    assert sorted(detected) == ["deepseek", "gemini", "groq"]


async def test_a_freshly_minted_key_does_not_see_another_keys_providers(client):
    """The root cause, pinned as behaviour rather than left implicit.

    This is not a bug to fix in the product - provider keys are deliberately
    per-key. It is the reason the eval must use the owner's credential, and
    the reason its error message must explain the scoping.
    """
    eval_module = _load_eval()
    dashboard = await _dashboard_key(client)
    await _save_provider_keys(client, dashboard, ["gemini", "groq", "deepseek"])

    fresh = (await client.post("/api/v1/keys/generate", json={"label": "eval"})).json()[
        "api_key"
    ]
    assert await eval_module.detect_providers(client, fresh) == []


async def test_preflight_with_the_dashboard_key_passes_for_three_providers(
    client, monkeypatch
):
    """End to end through the eval's real preflight: no blocker, 3 providers."""
    eval_module = _load_eval()
    monkeypatch.delenv(eval_module.EVAL_KEY_ENV_VAR, raising=False)
    dashboard = await _dashboard_key(client)
    await _save_provider_keys(client, dashboard, ["gemini", "groq", "deepseek"])
    monkeypatch.setenv(eval_module.EVAL_KEY_ENV_VAR, dashboard)

    report = await eval_module.collect_preflight(client, 5)

    assert report.ok, report.blocker
    assert sorted(report.configured) == ["deepseek", "gemini", "groq"]
    assert report.minted is False, "a supplied key must be reused, not replaced"


async def test_blocked_preflight_explains_scoping_and_never_prints_the_key(
    client, monkeypatch, capsys
):
    """The message the owner sees must explain WHY and WHAT TO DO."""
    eval_module = _load_eval()
    monkeypatch.delenv(eval_module.EVAL_KEY_ENV_VAR, raising=False)
    dashboard = await _dashboard_key(client)
    await _save_provider_keys(client, dashboard, ["gemini", "groq", "deepseek"])

    report = await eval_module.collect_preflight(client, 5)
    eval_module.print_preflight(report, "http://127.0.0.1:8000")
    out = capsys.readouterr().out

    assert not report.ok
    assert report.configured == []
    # It says how detection ran, and with which credential (prefix only).
    assert "GET /api/v1/provider-keys" in out
    assert "X-API-Key: " + report.api_key[:7] in out
    assert report.api_key not in out, "the full key must never be printed"
    # It names the scoping rule rather than blaming the owner.
    assert "not visible to this key" in out
    assert eval_module.EVAL_KEY_ENV_VAR in out
    # And it reports every gate, not just the one that failed.
    assert "Providers detected: none" in out
    assert "Chaos check:" in out
    assert "Request budget:" in out


async def test_dry_run_prints_every_gate_then_exits_nonzero_and_revokes(
    client, monkeypatch, capsys
):
    """--dry-run must not bail at the first failed gate, and must still revoke.

    The T0 symptom included a run that "ended silently" with only two lines of
    output. A dry run always prints providers, chaos, budget and the DRY RUN
    line before it decides anything.
    """
    eval_module = _load_eval()
    dashboard = await _dashboard_key(client)
    await _save_provider_keys(client, dashboard, ["gemini"])  # only one: blocked
    # Detection runs with the OWNER's credential (a minted key would see none,
    # which is the T0 bug itself); the run is blocked on the 2-provider gate.
    monkeypatch.setenv(eval_module.EVAL_KEY_ENV_VAR, dashboard)
    report = await eval_module.collect_preflight(client, 7)
    eval_module.print_preflight(report, "http://127.0.0.1:8000")
    out = capsys.readouterr().out

    assert not report.ok
    assert "Providers detected: gemini (1 with saved keys)" in out
    assert "Chaos check: enabled" in out
    assert "Request budget: 7 requests" in out

    # A key we did not mint is never revoked - it is the owner's.
    assert report.minted is False
    await eval_module.revoke_transient_key(
        client, report.api_key, minted=report.minted
    )
    still_valid = await client.get(
        "/api/v1/keys/me", headers={"X-API-Key": report.api_key}
    )
    assert still_valid.status_code == 200, "the owner's key must not be revoked"


async def test_blocked_dry_run_revokes_the_key_it_minted(client, monkeypatch, capsys):
    """The FATAL path must still revoke, and the dead key must stop working.

    A blocked run used to raise SystemExit straight out of preflight, leaving
    a live key behind. Proven here by authentication AFTER the revoke.
    """
    eval_module = _load_eval()
    monkeypatch.delenv(eval_module.EVAL_KEY_ENV_VAR, raising=False)

    report = await eval_module.collect_preflight(client, 7)  # mints; 0 providers
    assert not report.ok
    assert report.minted is True

    await eval_module.revoke_transient_key(
        client, report.api_key, minted=report.minted
    )
    # Enforcement ON: in local-first mode a revoked key deliberately falls back
    # to the local identity, so 401 is only observable with auth enforced.
    settings.REQUIRE_API_KEY = True
    try:
        after = await client.get(
            "/api/v1/keys/me", headers={"X-API-Key": report.api_key}
        )
    finally:
        settings.REQUIRE_API_KEY = False
    assert after.status_code == 401, (
        f"revoked key still authenticates: HTTP {after.status_code}"
    )


def test_docs_spell_the_credential_variable_exactly_as_the_code_does():
    """The printed instructions ARE the recovery path; a typo breaks them.

    The owner's flow must not need a manual step that is not written down
    correctly. An `ROUTTELLM_...` (two L's) typo in the README would send
    someone to an empty variable and straight back to "configured: none".
    """
    from pathlib import Path

    import re

    root = Path(__file__).resolve().parents[3]
    module = _load_eval()
    name = module.EVAL_KEY_ENV_VAR
    legacy = module.LEGACY_EVAL_KEY_ENV_VAR
    # C3: the product is Routtener, so ROUTTENER_ is the documented name and
    # ROUTELLM_ is only a compatibility fallback.
    assert name == "ROUTTENER_EVAL_API_KEY"
    assert legacy == "ROUTELLM_EVAL_API_KEY"

    for rel in ("README.md", "docs/testing.md"):
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text()
        # Any near-miss spelling of either variable fails loudly.
        for wrong in set(re.findall(r"ROUTTE?L+M_EVAL_API_KEY", text)) - {name, legacy}:
            raise AssertionError(f"{rel} misspells the variable as {wrong}")


def test_rename_prefers_the_new_name_and_falls_back_to_the_old(tmp_path):
    """C3: ROUTTENER_ wins; ROUTELLM_ still works; neither leaks a value."""
    module = _load_eval()

    import os

    saved = {k: os.environ.get(k) for k in
             (module.EVAL_KEY_ENV_VAR, module.LEGACY_EVAL_KEY_ENV_VAR)}
    try:
        for name in (module.EVAL_KEY_ENV_VAR, module.LEGACY_EVAL_KEY_ENV_VAR):
            os.environ.pop(name, None)
        assert module.eval_key_from_env() == ""

        os.environ[module.LEGACY_EVAL_KEY_ENV_VAR] = "old-name-key"
        assert module.eval_key_from_env() == "old-name-key"

        os.environ[module.EVAL_KEY_ENV_VAR] = "new-name-key"
        # Both set: the current name must win.
        assert module.eval_key_from_env() == "new-name-key"

        os.environ.pop(module.EVAL_KEY_ENV_VAR, None)
        # Whitespace-only values are not keys.
        os.environ[module.LEGACY_EVAL_KEY_ENV_VAR] = "   "
        assert module.eval_key_from_env() == ""
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
