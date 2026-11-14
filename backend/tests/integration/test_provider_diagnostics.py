"""T1: in-app diagnostics — POST /api/v1/provider-keys/{provider}/diagnostics.

Replaces all manual terminal work: one endpoint runs the list call, probes
the top 5 Auto candidates through the SAME walk adapter routing uses, and
reports everything the owner needs — with proof that no key, key fragment
or key-bearing URL can appear in the response, the logs or the report.

Fixture numbers are labelled: the 49-entries-before / 44-chat-after counts
are OBSERVED LIVE (the owner's own v1beta list call and the app's logged
models=44); the raw provider key below is a synthetic dummy.
"""

import httpx
import pytest
import re
from cryptography.fernet import Fernet

from app.api.routes import limiter
from app.config import settings
from app.core import model_discovery as md
from app.main import app
from app.providers import model_registry
from app.providers.base import ModelProvider, make_error, make_success

GEMINI_SECRET = "AIzaSyDUMMY_diagnostics_secret_123"

#: OBSERVED LIVE (owner's Codespace, real key): the raw v1beta list body
#: carried 49 entries; the app kept 44 after its non-chat filter (49−44=5,
#: the five TTS names).
OBSERVED_LIVE_RAW_COUNT = 49
OBSERVED_LIVE_CHAT_COUNT = 44


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


EIGHT_MODELS = [
    _model("gemini-3.8-flash", (3, 8)),
    _model("gemini-3.7-flash", (3, 7)),
    _model("gemini-3.6-flash", (3, 6)),
    _model("gemini-3.5-flash", (3, 5)),
    _model("gemini-3.5-flash-lite", (3, 5)),
    _model("gemini-2.5-flash", (2, 5)),
    _model("gemini-2.5-pro", (2, 5)),
    _model("gemini-2.5-flash-lite", (2, 5)),
]


class ScriptedGemini(ModelProvider):
    """Records calls; 3.6-flash 403s with a key-quoting message, rest 200."""

    provider_name = "gemini"
    DEFAULT_MODEL = "gemini-2.5-flash"

    def __init__(self):
        self.calls: list[str] = []
        self.kwargs_seen: list[dict] = []

    async def generate(self, prompt: str, **kwargs):
        model = kwargs.get("model", self.DEFAULT_MODEL)
        self.calls.append(model)
        self.kwargs_seen.append(kwargs)
        if model == "gemini-3.6-flash":
            # The provider echoing the key back — the worst case for redaction.
            return make_error(
                "gemini",
                model,
                f"auth_failed: gemini rejected the credential (HTTP 403). "
                f"Provider said: PERMISSION_DENIED for key {GEMINI_SECRET} "
                f"(Authorization: Bearer {GEMINI_SECRET}, url ?key={GEMINI_SECRET})",
                8.0,
                outcome="auth_failed",
                provider_status="PERMISSION_DENIED",
                provider_reason="CONSUMER_INVALID",
                http_status=403,
            )
        if model == "gemini-3.7-flash":
            return make_error(
                "gemini",
                model,
                "model_not_found: gemini has no model named 'gemini-3.7-flash' (HTTP 404).",
                9.0,
                outcome="model_not_found",
                provider_status="NOT_FOUND",
                http_status=404,
            )
        return make_success("gemini", model, f"{model} says hi", 5, 12.0)


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
    from app.core import test_endpoint_guard as guard

    guard._call_times.clear()
    guard._in_flight.clear()
    yield
    guard._call_times.clear()
    guard._in_flight.clear()
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
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac


async def _setup(client, monkeypatch, models=EIGHT_MODELS):
    """Save a key, stub the provider + discovery + live list counts."""
    stub = ScriptedGemini()
    from app.providers.registry import PROVIDER_REGISTRY

    monkeypatch.setitem(PROVIDER_REGISTRY, "gemini", stub)

    async def _discover(provider, api_key, force=False):
        assert api_key == GEMINI_SECRET
        return list(models), None

    monkeypatch.setattr(md, "discover_models", _discover)

    async def _fetch_counts(spec, api_key):
        assert api_key == GEMINI_SECRET
        return {
            "http_status": 200,
            "raw_count": OBSERVED_LIVE_RAW_COUNT,
            "chat_count": OBSERVED_LIVE_CHAT_COUNT,
            "error": None,
        }

    monkeypatch.setattr(model_registry.model_catalog, "fetch_counts", _fetch_counts)

    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t1"})
        ).json()["api_key"]
    }
    save = await client.put(
        "/api/v1/provider-keys/gemini",
        json={"api_key": GEMINI_SECRET},
        headers=headers,
    )
    assert save.status_code == 200, save.text
    return headers, stub


def _assert_no_secrets(text: str) -> None:
    assert GEMINI_SECRET not in text
    assert GEMINI_SECRET[8:20] not in text  # not even the middle of it
    # No unredacted key-bearing URL: "key=" may survive only when the value
    # was replaced with *** by redact() (the shape is still informative).
    assert not re.search(r"[?&]key=[A-Za-z0-9_\-]{6,}", text)


async def test_diagnostics_runs_and_reports_every_required_field(client, monkeypatch):
    headers, stub = await _setup(client, monkeypatch)
    response = await client.post(
        "/api/v1/provider-keys/gemini/diagnostics", headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # The wire facts the report exists for.
    assert body["list_http_status"] == 200
    assert body["list_count_before_filter"] == OBSERVED_LIVE_RAW_COUNT
    assert body["list_count_after_filter"] == OBSERVED_LIVE_CHAT_COUNT
    assert "x-goog-api-key" in body["key_transport"]
    assert "generativelanguage" in body["api_host"]
    assert body["api_version"] == "v1beta"
    assert body["received_host"] == "testserver"
    assert body["app_version"]
    assert body["python_version"]
    assert "Contains no keys" in body["notice"]

    # Rows: model id, HTTP status, provider identity, sanitized message, latency.
    assert body["probe_count"] == 5
    assert len(body["rows"]) == 5
    for row in body["rows"]:
        for field in (
            "model_id",
            "http_status",
            "provider_status",
            "provider_reason",
            "outcome",
            "latency_ms",
            "message",
        ):
            assert field in row
    by_id = {row["model_id"]: row for row in body["rows"]}
    # OBSERVED LIVE shape: a 403 with Google's error.status identity...
    assert by_id["gemini-3.6-flash"]["http_status"] == 403
    assert by_id["gemini-3.6-flash"]["provider_status"] == "PERMISSION_DENIED"
    assert by_id["gemini-3.6-flash"]["provider_reason"] == "CONSUMER_INVALID"
    # ...and a 404 model row.
    assert by_id["gemini-3.7-flash"]["http_status"] == 404
    assert by_id["gemini-3.7-flash"]["provider_status"] == "NOT_FOUND"
    assert by_id["gemini-3.8-flash"]["success"] is True
    assert by_id["gemini-3.8-flash"]["http_status"] == 200

    # Probes went through the walk adapter: no task preferences inherited.
    for kwargs in stub.kwargs_seen:
        assert "task_type" not in kwargs
        assert kwargs.get("max_tokens") == 1


async def test_diagnostics_probes_at_most_five_candidates(client, monkeypatch):
    headers, stub = await _setup(client, monkeypatch)
    response = await client.post(
        "/api/v1/provider-keys/gemini/diagnostics", headers=headers
    )
    assert response.status_code == 200
    # Eight discovered models — only the top 5 Auto candidates were probed.
    assert len(stub.calls) == 5
    # Rank order: newest versions first, and within a version the plain
    # "flash" preference group ranks before "flash-lite" (designed order).
    assert stub.calls == [
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.6-flash",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
    ]


async def test_diagnostics_never_leaks_a_key_in_response_logs_or_report(
    client, monkeypatch, capsys
):
    headers, stub = await _setup(client, monkeypatch)
    response = await client.post(
        "/api/v1/provider-keys/gemini/diagnostics", headers=headers
    )
    assert response.status_code == 200

    # 1. The whole response body.
    _assert_no_secrets(response.text)
    body = response.json()
    quoted = next(
        row for row in body["rows"] if row["model_id"] == "gemini-3.6-flash"
    )
    assert quoted["message"] is not None
    assert "PERMISSION_DENIED" in quoted["message"]  # the useful part survives
    _assert_no_secrets(quoted["message"])

    # 2. The clipboard report.
    report = body["report_markdown"]
    assert "Contains no keys" in report
    assert "| gemini-3.6-flash |" in report
    _assert_no_secrets(report)

    # 3. The logs.
    out = capsys.readouterr().out
    _assert_no_secrets(out)


async def test_diagnostics_report_is_valid_markdown_with_the_table(
    client, monkeypatch
):
    headers, _stub = await _setup(client, monkeypatch)
    response = await client.post(
        "/api/v1/provider-keys/gemini/diagnostics", headers=headers
    )
    body = response.json()
    report = body["report_markdown"]
    assert report.startswith("# Provider diagnostics — gemini")
    assert "Key transport:" in report
    assert "v1beta" in report
    # The header row and separator of the probe table are present.
    assert "| model | HTTP | provider status | provider reason | outcome | latency ms | message |" in report
    assert "|---|" in report


async def test_diagnostics_respects_the_per_key_guard(client, monkeypatch):
    headers, _stub = await _setup(client, monkeypatch)
    statuses = []
    for _ in range(11):
        response = await client.post(
            "/api/v1/provider-keys/gemini/diagnostics", headers=headers
        )
        statuses.append(response.status_code)
        if response.status_code == 429:
            assert "Retry-After" in response.headers
            break
    # Ten runs pass; the 11th is rate limited, never silent about it.
    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


async def test_diagnostics_without_a_stored_key_answers_404(client, monkeypatch):
    headers = {
        "X-API-Key": (
            await client.post("/api/v1/keys/generate", json={"label": "t1b"})
        ).json()["api_key"]
    }
    response = await client.post(
        "/api/v1/provider-keys/gemini/diagnostics", headers=headers
    )
    assert response.status_code == 404
