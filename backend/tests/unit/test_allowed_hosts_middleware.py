"""The Host-header allow-list middleware (DNS-rebinding defence).

A page on another origin can make the browser POST to any URL; if a DNS
rebinding attack points a public name at 127.0.0.1:8000, every loopback
service without a Host check answers it. The middleware's job: only the
loopback names, the configured bind host, and explicitly listed hosts get
through — everything else gets a plain 400 before CORS even runs.
"""

import httpx
import pytest

from app.config import settings


@pytest.fixture()
def enforce(monkeypatch):
    """Re-enable enforcement for these tests (the suite-wide fixture sets *)."""
    monkeypatch.setattr(settings, "ALLOWED_HOSTS", "")
    monkeypatch.setattr(settings, "HOST", "127.0.0.1")
    return monkeypatch


# --------------------------------------------------------------------------
# The decision function (covers parsing without a request round-trip)
# --------------------------------------------------------------------------


def test_loopback_names_are_allowed(enforce):
    from app.main import _host_allowed

    assert _host_allowed("localhost")
    assert _host_allowed("localhost:8000")
    assert _host_allowed("127.0.0.1")
    assert _host_allowed("127.0.0.1:8000")
    assert _host_allowed("::1")
    assert _host_allowed("[::1]")
    assert _host_allowed("[::1]:8000")


def test_configured_bind_host_is_allowed(enforce):
    from app.main import _host_allowed

    enforce.setattr(settings, "HOST", "0.0.0.0")
    assert _host_allowed("0.0.0.0:7860")
    assert _host_allowed("0.0.0.0")


def test_unknown_host_is_rejected(enforce):
    from app.main import _host_allowed

    assert not _host_allowed("attacker.example")
    assert not _host_allowed("evil.example:80")
    assert not _host_allowed("testserver")


def test_missing_host_header_is_rejected(enforce):
    """HTTP/1.1 requires Host; absence must not be an alias for 'yes'."""
    from app.main import _host_allowed

    assert not _host_allowed("")


def test_env_override_adds_deployed_hostnames(enforce):
    from app.main import _host_allowed

    enforce.setattr(settings, "ALLOWED_HOSTS", "my-space.hf.space, MyTunnel.example.com")
    assert _host_allowed("my-space.hf.space")
    assert _host_allowed("mytunnel.example.com:443")  # case + port handled
    assert not _host_allowed("still-evil.example")


def test_star_disables_enforcement(enforce):
    from app.main import _host_allowed

    enforce.setattr(settings, "ALLOWED_HOSTS", "*")
    assert _host_allowed("anything-goes.example")


def test_ipv6_without_brackets_is_not_corrupted_by_port_splitting(enforce):
    from app.main import _host_allowed

    # "fc00::1" has multiple colons: splitting on ":" would mangle it, so the
    # bare form is kept whole and matched as-is.
    assert not _host_allowed("fc00::1")  # not in the allow-list
    enforce.setattr(settings, "ALLOWED_HOSTS", "fc00::1")
    assert _host_allowed("fc00::1")


# --------------------------------------------------------------------------
# Through the real app: a 400 before routing, and other methods too
# --------------------------------------------------------------------------


async def test_unknown_host_gets_400_from_the_app(enforce):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/health", headers={"host": "attacker.example"})
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid Host header"}


async def test_loopback_host_gets_through_to_the_app(enforce):
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        ok = await ac.get("/health", headers={"host": "127.0.0.1:8000"})
        blocked = await ac.post(
            "/api/v1/route", headers={"host": "attacker.example"}, json={"prompt": "hi"}
        )
    assert ok.status_code != 400
    assert blocked.status_code == 400  # POST is gated the same way GET is


async def test_star_optout_disables_the_check(enforce):
    from app.main import app

    enforce.setattr(settings, "ALLOWED_HOSTS", "*")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        response = await ac.get("/health", headers={"host": "attacker.example"})
    assert response.status_code != 400
