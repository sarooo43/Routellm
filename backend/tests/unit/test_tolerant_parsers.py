"""Tolerant parsers + the debug shape capture (Step 3 safety net).

We have NO live provider access in the sandbox, so every fixture here is
SYNTHETIC, UNVERIFIED AGAINST THE LIVE API (each fixture file says so). The
contract under test: a hostile, broken, or merely different list-models body
must degrade to fewer or empty results — a parse failure never crashes the
endpoint, and no credential can ever land in the capture file.
"""

import json
from pathlib import Path

import httpx
import pytest

from app.providers.model_registry import (
    _capture_provider_shape,
    _extract_model_ids,
    model_catalog,
    spec_for,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "provider_shapes"


def _load_fixture(name: str) -> dict:
    """One fixture entry (each file is a list of labelled entries)."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))[0]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr("app.config.settings.ROUTELLM_CAPTURE_PROVIDER_SHAPES", False)
    model_catalog.clear()
    yield
    model_catalog.clear()


# --------------------------------------------------------------------------
# Recorded shapes (synthetic) parse exactly as documented
# --------------------------------------------------------------------------


def test_gemini_fixture_strips_prefix_and_filters_non_chat():
    body = _load_fixture("gemini_list_models.json")
    ids, meta = _extract_model_ids(spec_for("gemini"), body)
    assert "gemini-2.0-flash" in ids and "gemini-3.8-flash" in ids
    assert all(not m.startswith("models/") for m in ids)
    assert "text-embedding-004" not in ids  # embedContent only
    # Wire facts survive into the records.
    assert meta["gemini-2.0-flash"]["context_window"] == 1048576
    assert meta["gemini-2.0-flash"]["display_name"] == "Gemini 2.0 Flash"
    # Gemini gives NO created timestamp — the meta must not invent one.
    assert "created" not in meta["gemini-2.0-flash"]


def test_groq_fixture_keeps_created_and_context_and_drops_whisper():
    body = _load_fixture("groq_list_models.json")
    ids, meta = _extract_model_ids(spec_for("groq"), body)
    assert "llama-3.3-70b-versatile" in ids
    assert "whisper-large-v3-turbo" not in ids  # name-pattern non-chat filter
    assert meta["llama-3.3-70b-versatile"]["created"] == 1733533905
    assert meta["llama-3.3-70b-versatile"]["context_window"] == 131072


def test_deepseek_fixture_parses_bare_shape():
    body = _load_fixture("deepseek_list_models.json")
    ids, _meta = _extract_model_ids(spec_for("deepseek"), body)
    assert set(ids) == {"deepseek-chat", "deepseek-reasoner"}


# --------------------------------------------------------------------------
# Tolerance: broken bodies degrade, never raise
# --------------------------------------------------------------------------


@pytest.mark.parametrize("body", [
    None,
    {},
    [],
    "not a dict",
    {"data": None},
    {"data": "surprise"},
    {"models": [None, 5, True]},
    {"data": [{"id": ""}, {"id": "   "}, {"name": ""}]},
    {"data": [{"id": 42}]},
])
def test_broken_bodies_degrade_to_empty(body):
    ids, meta = _extract_model_ids(spec_for("groq"), body)
    assert ids == []
    assert meta == {}


def test_empty_list_is_empty_not_an_error():
    ids, _meta = _extract_model_ids(spec_for("groq"), {"data": []})
    assert ids == []


def test_huge_list_is_capped_and_deduped_case_insensitively():
    body = {
        "data": [
            {"id": f"model-{i:04d}-x"} for i in range(600)
        ]
        + [{"id": "MODEL-0000-X"}, {"id": "model-0000-x"}]  # dupes of #0
    }
    ids, _meta = _extract_model_ids(spec_for("groq"), body)
    assert len(ids) <= 500  # runaway lists cannot bloat the cache row
    lowered = [m.lower() for m in ids]
    assert len(lowered) == len(set(lowered))  # duplicates collapse


def test_prefix_and_no_prefix_both_parse():
    with_prefix = {"models": [{"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]}]}
    without_prefix = {"models": [{"name": "gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]}]}
    assert _extract_model_ids(spec_for("gemini"), with_prefix)[0] == ["gemini-2.0-flash"]
    assert _extract_model_ids(spec_for("gemini"), without_prefix)[0] == ["gemini-2.0-flash"]


def test_bare_string_entries_and_extra_fields_are_tolerated():
    body = {
        "data": [
            "llama-3.1-8b-instant",  # some OpenAI-compatible providers list strings
            {"id": "gemma2-9b-it", "future_unknown_field": {"nested": "data"}},
        ]
    }
    ids, meta = _extract_model_ids(spec_for("groq"), body)
    assert ids == ["llama-3.1-8b-instant", "gemma2-9b-it"]
    assert "future_unknown_field" not in str(meta)


def test_wrong_typed_numbers_are_dropped_not_crashed():
    body = {"data": [{"id": "m1", "created": "yesterday", "context_window": "big", "outputTokenLimit": None}]}
    ids, meta = _extract_model_ids(spec_for("groq"), body)
    assert ids == ["m1"]
    assert meta.get("m1", {}) == {}  # unknown fields degrade, nothing invented


# --------------------------------------------------------------------------
# The recorded error fixtures classify through the REAL outcome bridge
# --------------------------------------------------------------------------


def test_gemini_cohort_404_is_model_not_found_with_replacement_hint():
    from app.core.model_discovery import extract_replacement_model
    from app.providers.outcomes import error_for_status

    fixture = _load_fixture("gemini_error_model_not_found.json")
    body = json.dumps(fixture["body"])
    response = httpx.Response(
        fixture["status"], text=body, request=httpx.Request("POST", "https://generativelanguage.googleapis.com")
    )
    result = error_for_status(
        response, provider="gemini", model="gemini-2.0-flash", url="u", latency_ms=1.0
    )
    assert result.outcome == "model_not_found"
    # The replacement id named in the message is extracted and verified.
    hint = extract_replacement_model(
        (result.error or "").split(":", 1)[1],
        {"gemini-2.5-flash", "gemini-3.8-flash"},
        failed_model="gemini-2.0-flash",
    )
    assert hint == "gemini-2.5-flash"


def test_groq_decommissioned_404_is_model_not_found_not_auth():
    from app.providers.outcomes import error_for_status

    fixture = _load_fixture("groq_error_model_not_found.json")["model_decommissioned"]
    body = json.dumps(fixture["body"])
    response = httpx.Response(
        fixture["status"], text=body, request=httpx.Request("POST", "https://api.groq.com")
    )
    result = error_for_status(
        response, provider="groq", model="mixtral-8x7b-32768", url="u", latency_ms=1.0
    )
    assert result.outcome == "model_not_found"
    assert "auth" not in (result.outcome or "")


def test_deepseek_400_model_not_exist_bridges_to_model_not_found():
    from app.providers.outcomes import error_for_status

    fixture = _load_fixture("deepseek_error_model_missing.json")
    body = json.dumps(fixture["body"])
    response = httpx.Response(
        fixture["status"], text=body, request=httpx.Request("POST", "https://api.deepseek.com")
    )
    result = error_for_status(
        response, provider="deepseek", model="deepseek-coder", url="u", latency_ms=1.0
    )
    assert result.outcome == "model_not_found"


# --------------------------------------------------------------------------
# Debug capture: sanitized field names + redacted sample, never a credential
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, text: str, status_code: int = 200):
        self.text = text
        self.status_code = status_code


def test_capture_disabled_by_default_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.ROUTELLM_CAPTURE_PROVIDER_SHAPES", False)
    monkeypatch.setattr("app.local.data_dir", lambda create=True: tmp_path)
    _capture_provider_shape(
        "groq", "list_models", _FakeResponse('{"data": []}'), data={"data": []}
    )
    assert not (tmp_path / "provider_shapes").exists()


def test_capture_writes_field_names_and_redacted_sample(tmp_path, monkeypatch):
    import app.providers.model_registry as registry

    monkeypatch.setattr("app.config.settings.ROUTELLM_CAPTURE_PROVIDER_SHAPES", True)
    monkeypatch.setattr(registry, "data_dir", None, raising=False)
    monkeypatch.setattr("app.local.data_dir", lambda create=True: tmp_path)
    secret = "gsk_SECRETKEY9876543210"
    body = json.dumps(
        {"data": [{"id": "llama-3.1-8b-instant"}], "error": f"bad key {secret}"}
    )
    _capture_provider_shape("groq", "list_models", _FakeResponse(body), data=json.loads(body))

    target = tmp_path / "provider_shapes" / "groq.json"
    assert target.is_file()
    entries = json.loads(target.read_text(encoding="utf-8"))
    assert entries[0]["kind"] == "list_models"
    assert entries[0]["body_field_names"] == ["data", "error", "id"]
    # The key and any part of it must NEVER be written to disk.
    dumped = json.dumps(entries)
    assert secret not in dumped
    assert "9876" not in dumped
    assert "***" in entries[0]["body_sample"]  # redacted in the sample


def test_capture_of_an_error_response_is_also_redacted(tmp_path, monkeypatch):
    import app.providers.model_registry as registry

    monkeypatch.setattr("app.config.settings.ROUTELLM_CAPTURE_PROVIDER_SHAPES", True)
    monkeypatch.setattr(registry, "data_dir", None, raising=False)
    monkeypatch.setattr("app.local.data_dir", lambda create=True: tmp_path)
    secret = "AIzaSECRETKEY12345678"
    response = httpx.Response(
        401,
        text=json.dumps({"error": {"message": f"API key not valid: {secret}"}}),
        request=httpx.Request("GET", "https://generativelanguage.googleapis.com/v1beta/models"),
    )
    _capture_provider_shape("gemini", "list_models", response)

    dumped = (tmp_path / "provider_shapes" / "gemini.json").read_text(encoding="utf-8")
    assert secret not in dumped
    assert "Authorization" not in dumped  # header names/values never captured
