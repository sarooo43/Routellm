"""The operator script, tested the way an operator uses it.

Two properties matter more than the report layout:

* a key is never printed, and never accepted as a command-line argument;
* the verdict the script gives matches the app's own classification, because a
  script that disagrees with production is worse than no script.
"""

import importlib.util
import sys
from pathlib import Path

import httpx
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_providers.py"


def _load():
    """Import the script as a module (it is not part of the ``app`` package)."""
    spec = importlib.util.spec_from_file_location("check_providers", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_providers"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load()


# --------------------------------------------------------------------------
# venv bootstrap (friendly failure instead of a traceback)
# --------------------------------------------------------------------------


def test_bare_system_python_prints_the_friendly_message(monkeypatch):
    """No venv + ImportError = the mandated message, exit 1, no traceback."""
    script = _load()

    def _boom():
        raise ImportError("pydantic_settings")

    monkeypatch.setattr(script, "find_project_venv", lambda: None)
    with pytest.raises(SystemExit) as exc_info:
        script.ensure_app_importable(_boom)
    assert exc_info.value.code == 1


def test_no_reexec_loop_when_already_the_venv_python(monkeypatch, tmp_path, capsys):
    """The venv python failing to import says 'recreate it' — it never re-execs."""
    script = _load()
    fake_venv = tmp_path / "venv-python"
    fake_venv.write_text("")

    def _boom():
        raise ImportError("pydantic_settings")

    monkeypatch.setattr(script, "find_project_venv", lambda: fake_venv)
    # Pretend we ARE the venv python (the loop-guard branch).
    monkeypatch.setattr(script.sys, "executable", str(fake_venv))
    with pytest.raises(SystemExit) as exc_info:
        script.ensure_app_importable(_boom)
    assert exc_info.value.code == 1
    assert "Recreate it" in capsys.readouterr().out


def test_walk_reports_skipped_models_and_reasons(script, respx_mock):
    """--test output: the walk's SKIP lines name the denied model + reason."""
    from app.core import model_discovery as md

    md.clear_caches()
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "llama-3.1-8b-instant"}, {"id": "llama-3.3-70b-versatile"}]},
        )
    )
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(404, json={"error": {"message": "model_not_found"}})
    )
    secret = "gsk_TESTKEY1234567890"

    async def _run():
        async with httpx.AsyncClient() as client:
            result = script.Result("groq")
            result.key_masked = script.mask(secret)
            await script.run_walk(client, "groq", secret, result)
            return result

    import asyncio

    result = asyncio.run(_run())
    md.clear_caches()
    assert result.working_model is None
    outcomes = [step["outcome"] for step in result.walk]
    assert "failed" in outcomes  # the walk tried and hit model_not_found
    assert all("gsk_TESTKEY" not in str(step) for step in result.walk)


# --------------------------------------------------------------------------
# Masking
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key,expected", [
    ("gsk_abcdefghijklmnop1234", "gsk_…1234"),
    ("sk-proj-abcdefghijklmnop", "sk-…mnop"),
    ("AIzaSyABCDEFGHIJKLMNOP", "AIza…MNOP"),
])
def test_keys_are_masked(script, key, expected):
    got = script.mask(key)
    assert got == expected
    assert key not in got or len(key) <= 4


def test_no_key_prints_as_none(script):
    assert script.mask(None) == "(none)"


# --------------------------------------------------------------------------
# Key sourcing
# --------------------------------------------------------------------------


def test_env_beats_env_file(script, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("GROQ_API_KEY=gsk_fromfile1234\n")
    values = script.load_env_file(env_file)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_fromenv5678")
    assert script.key_for("groq", values) == "gsk_fromenv5678"


def test_env_file_is_used_when_env_is_unset(script, monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nexport GROQ_API_KEY='gsk_fromfile1234'\n")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert script.key_for("groq", script.load_env_file(env_file)) == "gsk_fromfile1234"


def test_blank_value_counts_as_no_key(script, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "   ")
    assert script.key_for("groq", {}) is None


def test_there_is_no_cli_flag_for_a_key(script):
    """Flags land in shell history and `ps`; keys must come from the env."""
    parser_source = SCRIPT.read_text()
    assert "--key" not in parser_source
    assert "add_argument(\"--api-key" not in parser_source


# --------------------------------------------------------------------------
# Verdicts, computed through the real classification
# --------------------------------------------------------------------------


async def test_working_provider_is_reported_valid(script, respx_mock):
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "whisper-large-v3"}, {"id": "llama-3.1-8b-instant"}]},
        )
    )
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
    )
    async with httpx.AsyncClient() as client:
        result = await script.check_provider(client, "groq", "gsk_TESTKEY1234567890", None)

    assert result.verdict == "valid"
    assert result.probe_ok is True
    assert result.key_accepted is True
    assert result.chosen == "llama-3.1-8b-instant"
    assert "whisper-large-v3" not in result.models


async def test_404_on_the_probe_is_model_not_found_not_auth(script, respx_mock):
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})
    )
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(404, json={"error": "model_not_found"})
    )
    async with httpx.AsyncClient() as client:
        result = await script.check_provider(client, "groq", "gsk_TESTKEY1234567890", None)

    assert result.key_accepted is True  # the key listed models fine
    assert result.verdict == "model_not_found"
    assert "llama-3.1-8b-instant" in result.probe_detail
    assert "api.groq.com" in result.probe_detail
    assert result.probe_ok is False


async def test_rejected_key_is_auth_failed(script, respx_mock):
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(401, json={"error": "invalid_api_key"})
    )
    async with httpx.AsyncClient() as client:
        result = await script.check_provider(client, "groq", "gsk_TESTKEY1234567890", None)

    assert result.key_accepted is False
    assert result.verdict == "auth_failed"
    assert result.chosen is None


async def test_unreachable_provider_is_network_error(script, respx_mock):
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        side_effect=httpx.ConnectError("blocked")
    )
    async with httpx.AsyncClient() as client:
        result = await script.check_provider(client, "groq", "gsk_TESTKEY1234567890", None)

    assert result.verdict == "network_error"
    assert "ConnectError" in result.probe_detail
    # The key was never even sent, so nothing may suggest it is wrong.
    assert "check your key" not in result.probe_detail.lower()


async def test_missing_key_is_not_configured(script):
    async with httpx.AsyncClient() as client:
        result = await script.check_provider(client, "groq", None, None)
    assert result.verdict == "not_configured"
    assert result.probe_ok is None


async def test_env_override_is_honoured(script, respx_mock):
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})
    )
    route = respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
    )
    async with httpx.AsyncClient() as client:
        result = await script.check_provider(client, "groq", "gsk_TESTKEY1234567890", "pinned-model")

    assert result.chosen == "pinned-model"
    assert result.model_source == "override"
    import json

    assert json.loads(route.calls[0].request.content)["model"] == "pinned-model"


async def test_gemini_uses_its_own_auth_header_and_body(script, respx_mock):
    respx_mock.get("https://generativelanguage.googleapis.com/v1beta/models").mock(
        return_value=httpx.Response(
            200,
            json={"models": [
                {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            ]},
        )
    )
    route = respx_mock.post(
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    ).mock(return_value=httpx.Response(200, json={"candidates": []}))

    async with httpx.AsyncClient() as client:
        result = await script.check_provider(client, "gemini", "AIzaTESTKEY12345678", None)

    assert result.chosen == "gemini-2.0-flash"
    assert "text-embedding-004" not in result.models
    request = route.calls[0].request
    assert request.headers["x-goog-api-key"] == "AIzaTESTKEY12345678"
    assert "authorization" not in {k.lower() for k in request.headers}
    import json

    assert "contents" in json.loads(request.content)


# --------------------------------------------------------------------------
# Output never leaks
# --------------------------------------------------------------------------


async def test_the_printed_report_never_contains_the_key(script, respx_mock, capsys):
    secret = "gsk_VERYSECRETKEY9876543210"
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})
    )
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(401, text=f'{{"error":"bad key {secret}"}}')
    )
    async with httpx.AsyncClient() as client:
        result = await script.check_provider(client, "groq", secret, None)

    script.print_result(result)
    for output in (capsys.readouterr().out, result.as_dict().__str__(), str(result.as_dict())):
        assert secret not in output
        assert "9876" not in output  # not even the middle of it


async def test_the_provider_echo_of_a_key_is_redacted_in_the_detail(script, respx_mock):
    secret = "gsk_VERYSECRETKEY9876543210"
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})
    )
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(401, text=f'{{"error":"bad key {secret}"}}')
    )
    async with httpx.AsyncClient() as client:
        result = await script.check_provider(client, "groq", secret, None)
    assert secret not in result.probe_detail


# --------------------------------------------------------------------------
# Exit codes
# --------------------------------------------------------------------------


async def test_exit_code_zero_when_everything_works(script, respx_mock, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_TESTKEY1234567890")
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})
    )
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
    )
    assert await script.main_async(["groq", "--env-file", "/nonexistent"]) == 0


async def test_exit_code_one_when_a_provider_fails(script, respx_mock, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_TESTKEY1234567890")
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(401, json={"error": "nope"})
    )
    assert await script.main_async(["groq", "--env-file", "/nonexistent"]) == 1


async def test_exit_code_two_when_nothing_is_configured(script, monkeypatch):
    for provider in ("GROQ", "GEMINI", "DEEPSEEK"):
        monkeypatch.delenv(f"{provider}_API_KEY", raising=False)
    assert await script.main_async(["groq", "--env-file", "/nonexistent"]) == 2


async def test_unknown_provider_is_a_usage_error(script):
    assert await script.main_async(["not-a-provider"]) == 2


async def test_json_mode_is_parseable_and_safe(script, respx_mock, monkeypatch, capsys):
    import json

    monkeypatch.setenv("GROQ_API_KEY", "gsk_TESTKEY1234567890")
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})
    )
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "pong"}}]})
    )
    assert await script.main_async(["groq", "--env-file", "/nonexistent", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["provider"] == "groq"
    assert payload[0]["verdict"] == "valid"
    assert "gsk_TESTKEY1234567890" not in json.dumps(payload)


# --------------------------------------------------------------------------
# --report: the T1 diagnostics file (gitignored, contains no keys)
# --------------------------------------------------------------------------


async def test_report_flag_writes_a_markdown_report_with_no_keys(
    script, respx_mock, monkeypatch, tmp_path, capsys
):
    """--report writes data/diagnostics/report.md; the file never has a key."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_TESTKEY1234567890")
    respx_mock.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "llama-3.1-8b-instant"}]})
    )
    # The provider echoes the key back — the worst case for the report sweep.
    respx_mock.post("https://api.groq.com/openai/v1/chat/completions").mock(
        return_value=httpx.Response(
            401, json={"error": {"message": f"bad key gsk_TESTKEY1234567890"}}
        )
    )
    monkeypatch.setenv("ROUTELLM_DATA_DIR", str(tmp_path))
    assert await script.main_async(["groq", "--env-file", "/nonexistent", "--report"]) == 1

    printed = capsys.readouterr().out
    assert "report written:" in printed
    report_path = tmp_path / "diagnostics" / "report.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert content.startswith("# Provider diagnostics report")
    assert "Contains no keys" in content
    assert "gsk_TESTKEY1234567890" not in content
    assert "TESTKEY" not in content  # not even a fragment
    assert "Key accepted" in content
