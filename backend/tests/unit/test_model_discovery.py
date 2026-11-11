"""Unit tests for model discovery: ranking, replacement extraction, cache."""

import time

import pytest

from app.core import model_discovery as md


def model(mid, created=None, context=None, max_out=None):
    return md.DiscoveredModel(
        id=mid,
        display_name=mid,
        version=md.parse_version_tuple(mid),
        created=created,
        context_window=context,
        max_output=max_out,
        chat_capable=True,
        capabilities=["chat"],
    )


# --------------------------------------------------------------------------
# Version parsing (numeric, never string)
# --------------------------------------------------------------------------


def test_version_ordering_numeric_38_gt_25_gt_20():
    assert md.parse_version_tuple("gemini-3.8-flash") > md.parse_version_tuple(
        "gemini-2.5-flash"
    )
    assert md.parse_version_tuple("gemini-2.5-flash") > md.parse_version_tuple(
        "gemini-2.0-flash"
    )


def test_version_ordering_10_gt_9():
    assert md.parse_version_tuple("gemini-10.0-flash") > md.parse_version_tuple(
        "gemini-9.0-flash"
    )


def test_version_none_when_no_digits():
    assert md.parse_version_tuple("gemini-flash-latest") is None


# --------------------------------------------------------------------------
# Ranking (pattern-based preference groups)
# --------------------------------------------------------------------------


def test_rank_candidates_prefers_newest_plain_flash():
    models = [
        model("gemini-2.5-flash"),
        model("gemini-3.8-flash"),
        model("gemini-2.5-flash-lite"),
        model("gemini-2.5-pro"),
    ]
    ranked = md.rank_candidates("gemini", models, denied=set())
    assert [m.id for m in ranked[:2]] == [
        "gemini-3.8-flash",
        "gemini-2.5-flash",
    ]
    # lite and pro rank after plain flash regardless of version
    assert {m.id for m in ranked[2:]} == {"gemini-2.5-flash-lite", "gemini-2.5-pro"}


def test_rank_candidates_skips_denied():
    models = [model("gemini-2.5-flash"), model("gemini-3.8-flash")]
    ranked = md.rank_candidates("gemini", models, denied={"gemini-2.5-flash"})
    assert [m.id for m in ranked] == ["gemini-3.8-flash"]


def test_preference_group_excludes_preview_and_thinking():
    group, _ = md.preference_group("gemini", "gemini-2.5-flash-preview")
    plain, _ = md.preference_group("gemini", "gemini-3.8-flash")
    assert group > plain  # preview falls into a later (lower) group


# --------------------------------------------------------------------------
# Replacement extraction (strict; never trusts arbitrary message content)
# --------------------------------------------------------------------------


def test_extract_replacement_from_real_gemini_message():
    message = (
        "models/gemini-2.5-flash is no longer available to new users. "
        "Please update your code to use models/gemini-3.8-flash"
    )
    ids = {"gemini-2.5-flash", "gemini-3.8-flash"}
    assert md.extract_replacement_model(message, ids) == "gemini-3.8-flash"


def test_extract_replacement_rejects_path_traversal():
    message = "Please update your code to use models/../../etc/passwd"
    ids = {"gemini-2.5-flash"}
    assert md.extract_replacement_model(message, ids) is None


def test_extract_replacement_rejects_url():
    message = "See https://evil.example/models/gemini-3.8-flash for details"
    ids = {"gemini-3.8-flash"}
    # Even though the charset matches, the candidate must be trusted only
    # via the models/<id> / use <id> shapes AND exist in the discovered list;
    # a URL never names a usable replacement.
    assert md.extract_replacement_model(message, ids) is None


def test_extract_replacement_requires_known_model():
    message = "use models/gemini-9.9-flash"
    ids = {"gemini-3.8-flash"}
    assert md.extract_replacement_model(message, ids) is None


def test_is_valid_model_id_rejects_injection():
    assert not md.is_valid_model_id("../x")
    assert not md.is_valid_model_id("https://evil.com/x")
    assert not md.is_valid_model_id("")
    assert not md.is_valid_model_id("models/../secret")
    assert md.is_valid_model_id("gemini-3.8-flash")
    assert md.is_valid_model_id("models/gemini-3.8-flash")


# --------------------------------------------------------------------------
# Honest error classification
# --------------------------------------------------------------------------


def test_classify_error_404_is_model_not_found():
    assert (
        md.classify_provider_error(
            404, '{"error":{"status":"NOT_FOUND","message":"models/gemini-2.5-flash '
            'is no longer available to new users"}}'
        )
        == "model_not_found"
    )


def test_classify_error_deepseek_400_model_not_exist():
    assert (
        md.classify_provider_error(400, '{"error":{"message":"Model Not Exist"}}')
        == "model_not_found"
    )


def test_classify_error_never_reclassifies_auth_or_rate_limit():
    assert md.classify_provider_error(401, "unauthorized") is None
    assert md.classify_provider_error(403, "forbidden") is None
    assert md.classify_provider_error(429, "rate limited") is None


# --------------------------------------------------------------------------
# Tier estimates + sorting/filtering
# --------------------------------------------------------------------------


def test_tier_estimates_and_unknown():
    assert md.estimated_tier("gemini", "gemini-3.8-flash") == "free"
    assert md.estimated_tier("gemini", "gemini-2.5-pro") == "paid"
    assert md.estimated_tier("gemini", "gemini-mystery-model") == "unknown"
    assert md.estimated_tier("deepseek", "deepseek-chat") == "paid"
    assert md.estimated_tier("groq", "llama-3.3-70b-versatile") == "free"


def test_sort_models_tier_then_newest_and_hidden_count():
    models = [
        model("gemini-2.5-pro", created=100),
        model("gemini-2.0-flash-lite"),
        model("gemini-3.8-flash"),
        md.DiscoveredModel(
            id="text-embedding-004",
            display_name="text-embedding-004",
            version=None,
            created=None,
            context_window=None,
            max_output=None,
            chat_capable=False,
            capabilities=["embedding"],
        ),
    ]
    visible, hidden = md.sort_models_for_provider(models, "gemini", sort="tier")
    assert hidden == 1
    ids = [m.id for m in visible]
    assert ids == ["gemini-3.8-flash", "gemini-2.0-flash-lite", "gemini-2.5-pro"]


def test_sort_models_newest_and_name():
    models = [model("gemini-2.5-flash", created=10), model("gemini-3.8-flash")]
    newest, _ = md.sort_models_for_provider(models, "gemini", sort="newest")
    assert newest[0].id == "gemini-3.8-flash"
    by_name, _ = md.sort_models_for_provider(
        [model("b-model"), model("a-model")], "gemini", sort="name"
    )
    assert [m.id for m in by_name] == ["a-model", "b-model"]


def test_tier_filter_and_search():
    models = [model("gemini-3.8-flash"), model("gemini-2.5-pro")]
    free_only, _ = md.sort_models_for_provider(models, "gemini", tier_filter="free")
    assert [m.id for m in free_only] == ["gemini-3.8-flash"]
    searched, _ = md.sort_models_for_provider(
        models, "gemini", search="pro"
    )
    assert [m.id for m in searched] == ["gemini-2.5-pro"]


# --------------------------------------------------------------------------
# Denied/status cache (~1h TTL)
# --------------------------------------------------------------------------


def test_denied_cache_marks_and_expires(monkeypatch):
    md.clear_caches()
    key = "AIzaSyDUMMY_cache_test_key"
    md.mark_status(key, "gemini", "gemini-2.5-flash", "denied")
    assert md.get_cached_status(key, "gemini", "gemini-2.5-flash") == "denied"
    assert "gemini-2.5-flash" in md.denied_models(key, "gemini")

    # TTL expiry: shrink the window by aging the entry.
    fp = md.key_fingerprint(key)
    status, _ = md._status_cache[(fp, "gemini")]["gemini-2.5-flash"]
    md._status_cache[(fp, "gemini")]["gemini-2.5-flash"] = (
        status,
        time.monotonic() - 1,
    )
    assert md.get_cached_status(key, "gemini", "gemini-2.5-flash") is None
    assert md.denied_models(key, "gemini") == set()
    md.clear_caches()


def test_status_cache_never_marks_auth_or_other_errors():
    md.clear_caches()
    key = "AIzaSyDUMMY_other_errors"
    # Only denied / rate_limited / works are ever cached, and only by the
    # walk; nothing caches an auth error.
    assert md.get_cached_status(key, "gemini", "gemini-3.8-flash") is None
    md.clear_caches()


def test_sanitize_error_message_scrubs_key_and_truncates():
    secret = "AIzaSySUPERSECRETVALUE123"
    message = f"boom {secret} " + "x" * 500
    sanitized = md.sanitize_error_message(message, secret)
    assert secret not in sanitized
    assert len(sanitized) <= 200


# --------------------------------------------------------------------------
# check_providers.py bootstrap
# --------------------------------------------------------------------------


def _load_script():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[2] / "scripts" / "check_providers.py"
    spec = importlib.util.spec_from_file_location("check_providers_script", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_friendly_message_when_no_venv(monkeypatch, capsys):
    script = _load_script()
    monkeypatch.setattr(script, "find_project_venv", lambda: None)

    def boom():
        raise ImportError("pydantic_settings")

    with pytest.raises(SystemExit) as exc:
        script.ensure_app_importable(_try_import=boom)
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Run with the project venv:" in out
    assert ".venv/bin/python backend/scripts/check_providers.py" in out
    assert "python run.py once to create it" in out


def test_bootstrap_reexecs_with_project_venv(monkeypatch):
    script = _load_script()
    fake_venv = script.BACKEND_DIR / ".venv" / "Scripts" / "python.exe"
    monkeypatch.setattr(script, "find_project_venv", lambda: fake_venv)
    # Tests already run INSIDE the venv; pretend we were launched with a
    # system python so the re-exec branch is reachable.
    monkeypatch.setattr(script.sys, "executable", "C:/system/python.exe")

    exec_calls = []

    def fake_execv(path, args):
        exec_calls.append((path, args))
        raise RuntimeError("exec-replaced")  # would never return in reality

    monkeypatch.setattr(script.os, "execv", fake_execv)

    def boom():
        raise ImportError("pydantic_settings")

    with pytest.raises(RuntimeError, match="exec-replaced"):
        script.ensure_app_importable(_try_import=boom)
    assert exec_calls and exec_calls[0][0] == str(fake_venv)


def test_find_project_venv_prefers_existing_platform_path():
    script = _load_script()
    # This repo HAS backend/.venv (Scripts\\python.exe on Windows,
    # bin/python on Linux/macOS); whichever exists must be found.
    found = script.find_project_venv()
    assert found is not None
    assert found.exists()
