#!/usr/bin/env python3
"""Ask each provider what it actually thinks of your key.

Answers the question that started this: *"the Providers page says my key is
valid, so why does every route fail?"* That page only knew whether a row
existed. This script performs the same three calls routing performs, in order,
and prints what came back:

1. ``GET /models`` with your key — is the credential accepted, and which chat
   models does this account actually offer?
2. Which of those models the app would choose (same preference order the
   router uses, imported from ``app.providers.model_registry`` so the script
   cannot drift from production).
3. A real one-token chat request to that model — does the whole path work?

Design rules:

* **Keys come from the environment or ``.env`` only.** Never a CLI flag: flags
  land in shell history and ``ps`` output. Output is masked to the last 4.
* **Read-only about credentials.** No files written except the app's own
  discovery cache row (fingerprints + model ids — never a key); ``--test``
  reuses the app's walk so the report cannot drift from production.
* **Honest exit code.** 0 = every configured provider works, 1 = at least one
  failed, 2 = nothing to check.

Usage::

    python scripts/check_providers.py                  # all providers
    python scripts/check_providers.py groq gemini      # only these
    python scripts/check_providers.py --json           # machine-readable
    python scripts/check_providers.py --test           # + the candidate walk

Requires ``httpx`` (already a backend dependency). Run from ``backend/`` or
point ``PYTHONPATH`` at it. If launched with a bare system Python that lacks
the app dependencies, the script re-execs itself inside the project venv
(``backend/.venv``) or prints the friendly command to run instead — never a
raw traceback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Optional

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent

# Make `app` importable when run as `python backend/scripts/check_providers.py`.
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

#: The friendly message mandated for a bare system-Python run without a venv.
VENV_MESSAGE = (
    "Run with the project venv: .venv/bin/python backend/scripts/check_providers.py, "
    "or run python run.py once to create it."
)


def find_project_venv() -> Optional[Path]:
    """Locate the project's virtualenv python, Linux/macOS or Windows."""
    candidates = [
        BACKEND_ROOT / ".venv" / "bin" / "python",
        BACKEND_ROOT / ".venv" / "Scripts" / "python.exe",
        BACKEND_ROOT / ".venv" / "Scripts" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def ensure_app_importable(_try_import=None) -> None:
    """Bootstrap: re-exec inside the project venv when system Python lacks
    the app dependencies (pydantic_settings etc.). Never a raw traceback.

    ``_try_import`` is injectable for tests; production uses the default
    import of the app's dependency set.
    """
    def _default() -> None:
        import pydantic_settings  # noqa: F401
        import app.config  # noqa: F401,E402

    try:
        (_try_import or _default)()
    except ImportError:
        venv_python = find_project_venv()
        if venv_python is None:
            print(VENV_MESSAGE)
            if os.name == "nt":
                print(
                    "Windows: backend\\.venv\\Scripts\\python.exe "
                    "backend\\scripts\\check_providers.py"
                )
            sys.exit(1)
        if Path(sys.executable).resolve() == venv_python.resolve():
            # Already the venv python and still failing: no re-exec loop.
            print(
                "The project venv cannot import the app dependencies. "
                "Recreate it: delete backend/.venv, then run "
                "python -m venv backend/.venv and pip install -r backend/requirements-dev.txt"
            )
            sys.exit(1)
        # Re-exec THIS script with the venv interpreter (argv preserved).
        os.execv(
            str(venv_python), [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]]
        )


ensure_app_importable()

import httpx  # noqa: E402

from app.providers.model_registry import (  # noqa: E402
    PROVIDER_SPECS,
    choose_model,
    estimated_tier,
    is_chat_capable,
    parse_version_tuple,
    rank_candidate_ids,
    spec_for,
)
from app.providers.outcomes import (  # noqa: E402
    classify_status,
    error_for_status,
    redact,
)

PROBE_MAX_TOKENS = 1
TIMEOUT_S = 20.0
CHAT_MODELS_SHOWN = 10


# --------------------------------------------------------------------------
# Key loading — environment first, then .env, never argv
# --------------------------------------------------------------------------

def mask(key: Optional[str]) -> str:
    """``gsk_abcdef…1234`` — enough to identify a key, not enough to use it."""
    if not key:
        return "(none)"
    key = key.strip()
    if len(key) <= 4:
        return "*" * len(key)
    prefix = ""
    for candidate in ("gsk_", "sk-", "AIza", "hf_"):
        if key.startswith(candidate):
            prefix = candidate
            break
    return f"{prefix}…{key[-4:]}"


def load_env_file(path: Path) -> dict[str, str]:
    """Minimal .env reader: ``KEY=value``, ignores comments and export."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name = name.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if name:
            values[name] = value
    return values


def key_for(provider: str, file_values: dict[str, str]) -> Optional[str]:
    """Environment wins over .env, matching what the app itself loads."""
    env_name = f"{provider.upper()}_API_KEY"
    value = os.environ.get(env_name) or file_values.get(env_name)
    return (value or "").strip() or None


def model_override_for(provider: str, file_values: dict[str, str]) -> Optional[str]:
    name = f"{provider.upper()}_MODEL"
    return (os.environ.get(name) or file_values.get(name) or "").strip() or None


# --------------------------------------------------------------------------
# Per-provider check
# --------------------------------------------------------------------------

class Result:
    def __init__(self, provider: str):
        self.provider = provider
        self.key_masked = "(none)"
        self.status: Optional[int] = None
        self.key_accepted: Optional[bool] = None
        self.models: list[str] = []
        #: Same sorted list, enriched with tier/version (explorer columns).
        self.models_detailed: list[dict] = []
        #: --test only: the candidate walk report (skipped models + reasons).
        self.walk: list[dict] = []
        self.working_model: Optional[str] = None
        self.chosen: Optional[str] = None
        self.model_source: Optional[str] = None
        self.probe_ok: Optional[bool] = None
        self.probe_detail: str = ""
        self.verdict = "not_configured"

    def as_dict(self) -> dict:
        payload = {
            "provider": self.provider,
            "key": self.key_masked,
            "key_accepted": self.key_accepted,
            "status": self.status,
            "chat_models": self.models,
            "model": self.chosen,
            "model_source": self.model_source,
            "probe_ok": self.probe_ok,
            "verdict": self.verdict,
            "detail": self.probe_detail,
        }
        if self.models_detailed:
            payload["chat_models_detailed"] = self.models_detailed
        if self.walk:
            payload["walk"] = self.walk
        return payload


def _headers(provider: str, key: str) -> dict[str, str]:
    if provider == "gemini":
        return {"x-goog-api-key": key}
    return {"Authorization": f"Bearer {key}"}


async def list_models(client: httpx.AsyncClient, provider: str, key: str, result: Result) -> list[str]:
    spec = PROVIDER_SPECS[provider]
    try:
        response = await client.get(spec.models_url(), headers=_headers(provider, key))
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        result.status = None
        result.probe_detail = f"network: {type(exc).__name__}: {redact(str(exc))}"
        return []

    result.status = response.status_code
    result.key_accepted = response.status_code == 200
    if response.status_code != 200:
        # Same builder routing uses, so the script cannot describe a status
        # differently from the app.
        failure = error_for_status(
            response, provider=provider, model="", url=spec.models_url(), latency_ms=0.0
        )
        result.probe_detail = failure.error or classify_status(response.status_code)
        return []

    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a 200 with a broken body
        result.probe_detail = "200 but the body was not JSON"
        return []

    raw = payload.get("data") or payload.get("models") or []
    ids: list[str] = []
    for item in raw:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or ""
        else:
            continue
        model_id = model_id.removeprefix("models/")
        if not model_id:
            continue
        methods = item.get("supportedGenerationMethods") if isinstance(item, dict) else None
        if methods and "generateContent" not in methods:
            continue
        if not is_chat_capable(spec, model_id):
            continue
        ids.append(model_id)
    return ids


async def probe(client: httpx.AsyncClient, provider: str, key: str, model: str, result: Result) -> None:
    """One real one-token request — the same call routing makes."""
    spec = PROVIDER_SPECS[provider]
    url = spec.chat_url(model)
    headers = _headers(provider, key)
    headers["Content-Type"] = "application/json"

    if provider == "gemini":
        body = {
            "contents": [{"role": "user", "parts": [{"text": "ping"}]}],
            "generationConfig": {"maxOutputTokens": PROBE_MAX_TOKENS},
        }
    else:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": PROBE_MAX_TOKENS,
        }

    try:
        response = await client.post(url, json=body, headers=headers)
    except Exception as exc:  # noqa: BLE001
        result.probe_ok = False
        result.verdict = "network_error"
        result.probe_detail = f"{type(exc).__name__}: {redact(str(exc))}"
        return

    if response.status_code == 200:
        result.probe_ok = True
        result.verdict = "valid"
        result.probe_detail = "1-token reply received"
        return

    result.probe_ok = False
    # Refined by the same code as production: a 404 becomes model_not_found
    # with the model name and host, not a bare HTTP code.
    failure = error_for_status(response, provider=provider, model=model, url=url, latency_ms=0.0)
    result.verdict = failure.outcome or classify_status(response.status_code)
    result.probe_detail = failure.error or f"HTTP {response.status_code}"


async def check_provider(
    client: httpx.AsyncClient,
    provider: str,
    key: Optional[str],
    override: Optional[str],
) -> Result:
    result = Result(provider)
    if not key:
        result.probe_detail = f"{provider.upper()}_API_KEY is not set"
        return result

    result.key_masked = mask(key)
    models = await list_models(client, provider, key, result)
    result.models = models
    # The explorer's columns: the ONE shared ranking, plus the static tier
    # estimate (never presented as provider data) and the parsed version.
    spec = PROVIDER_SPECS[provider]
    for model_id in rank_candidate_ids(spec, models):
        version = parse_version_tuple(model_id)
        result.models_detailed.append(
            {
                "id": model_id,
                "tier": estimated_tier(provider, model_id),
                "version": ".".join(str(p) for p in version) if version else None,
                "status": "untested",
            }
        )

    if result.key_accepted is False:
        result.verdict = classify_status(result.status or 0)
        return result

    if result.key_accepted is None:
        # The list call never got an answer, so the network is the problem and
        # sending a chat request would fail the same way. Report it once.
        result.verdict = "network_error"
        return result

    if override:
        result.chosen, result.model_source = override, "override"
    elif models:
        result.chosen, result.model_source = choose_model(PROVIDER_SPECS[provider], models), "discovered"
    else:
        # Discovery failed, so this is the static fallback — say so rather than
        # claiming it was discovered.
        result.chosen, result.model_source = PROVIDER_SPECS[provider].default_model, "default"

    await probe(client, provider, key, result.chosen, result)
    return result


async def run_walk(client: httpx.AsyncClient, provider: str, key: str, result: Result) -> None:
    """--test: the same candidate walk real routing uses, reported step by step.

    Uses ``app.core.model_discovery.walk_candidates`` (the ONE walk routing
    uses) so the script cannot disagree with production. Skipped models are
    printed with their reasons; the working model (if any) is named.
    """
    from app.core.model_discovery import walk_candidates  # noqa: E402

    walk = await walk_candidates(
        provider,
        "ping",
        {"provider_api_key": key, "max_tokens": PROBE_MAX_TOKENS},
        key,
        first_model=None,
        max_attempts=3,
    )
    working = None
    for step in walk.steps:
        entry = {"model": step.model, "skipped": step.skipped, "reason": step.reason}
        if step.skipped:
            entry["outcome"] = "skipped"
        elif step.success:
            entry["outcome"] = "ok"
            working = step.model
        else:
            entry["outcome"] = "failed"
        result.walk.append(entry)
    result.working_model = working


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def print_result(result: Result) -> None:
    spec = PROVIDER_SPECS[result.provider]
    line = "=" * 68
    print(line)
    print(f"{result.provider.upper()}  ({spec.base_url})")
    print(f"  key            {result.key_masked}")
    if result.key_accepted is None:
        print(f"  key accepted   unknown")
    else:
        accepted = "YES" if result.key_accepted else f"NO ({result.status})"
        print(f"  key accepted   {accepted}")
    print(f"  models URL     {spec.models_url()}")

    if result.models:
        print(f"  chat models    {len(result.models)} available, explorer order (first {min(len(result.models), CHAT_MODELS_SHOWN)}):")
        for entry in result.models_detailed[:CHAT_MODELS_SHOWN]:
            version = entry.get("version") or "-"
            tier = entry.get("tier") or "unknown"
            print(
                f"                 - {entry['id']:<42} tier={tier:<8} version={version:<10} status={entry['status']}"
            )
    elif result.key_accepted:
        print("  chat models    none listed for this key")
    else:
        print("  chat models    could not be listed")

    if result.chosen:
        print(f"  would choose   {result.chosen}  ({result.model_source})")
        print(f"  chat URL       {spec.chat_url(result.chosen)}")

    if result.probe_ok is None:
        print(f"  probe          skipped")
    else:
        print(f"  probe          {'OK' if result.probe_ok else 'FAILED'} — {result.verdict}")
    if result.probe_detail:
        print(f"  detail         {result.probe_detail}")

    # A plain-language summary, because the whole point of this script is that
    # the previous UI summary was wrong.
    if result.verdict == "valid":
        print("  => this provider works end to end from this machine.")
    elif result.verdict == "auth_failed":
        print("  => the provider rejected THIS KEY. Only now is 'check your key' the right advice.")
    elif result.verdict in {"model_not_found", "endpoint_not_found"}:
        print("  => the key is fine but the model or URL is not. Pick a model from the list above.")
    elif result.verdict == "network_error":
        print("  => this machine could not reach the provider. That is not a key problem.")
    elif result.verdict == "not_configured":
        print(f"  => no key to test. Set {result.provider.upper()}_API_KEY.")
    else:
        print(f"  => {result.verdict}; see detail above.")


async def main_async(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("providers", nargs="*", help=f"subset of: {', '.join(PROVIDER_SPECS)}")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a report")
    parser.add_argument(
        "--test",
        action="store_true",
        help="also run the candidate walk real routing uses (prints skipped models and reasons)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="write a Markdown diagnostics report to data/diagnostics/report.md (gitignored, contains no keys)",
    )
    parser.add_argument(
        "--env-file",
        default=str(REPO_ROOT / ".env"),
        help="fallback .env path (default: repository root)",
    )
    args = parser.parse_args(argv)

    providers = args.providers or list(PROVIDER_SPECS)
    unknown = [p for p in providers if p not in PROVIDER_SPECS]
    if unknown:
        print(f"unknown provider(s): {', '.join(unknown)}", file=sys.stderr)
        return 2

    file_values = load_env_file(Path(args.env_file))
    configured = {p: key_for(p, file_values) for p in providers}
    if not any(configured.values()):
        print("No provider keys found in the environment or .env.", file=sys.stderr)
        print("Set e.g. GROQ_API_KEY=gsk_... (see .env.example).", file=sys.stderr)
        return 2

    results: list[Result] = []
    async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
        for provider in providers:
            result = await check_provider(
                client, provider, configured[provider], model_override_for(provider, file_values)
            )
            if args.test and configured[provider]:
                await run_walk(client, provider, configured[provider], result)
                if not args.json:
                    _print_walk(result)
            results.append(result)

    if args.json:
        print(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        for result in results:
            print_result(result)
        print("=" * 68)
        working = [r.provider for r in results if r.verdict == "valid"]
        broken = [r.provider for r in results if r.verdict not in {"valid", "not_configured"}]
        print(f"working: {', '.join(working) or 'none'}")
        if broken:
            print(f"failing: {', '.join(broken)}")

    if args.report:
        from app.local import data_dir

        path = write_report(results, data_dir(create=True) / "diagnostics" / "report.md")
        print(f"report written: {path}")

    return 0 if all(r.verdict in {"valid", "not_configured"} for r in results) else 1


def _print_walk(result: "Result") -> None:
    """The walk report in text mode: OK / FAIL / SKIP per candidate."""
    print(f"  candidate walk ({result.provider}):")
    for step in result.walk:
        marker = {"ok": "OK  ", "failed": "FAIL", "skipped": "SKIP"}[step["outcome"]]
        print(f"    {marker} {step['model']}: {step['reason'] or 'worked'}")
    if result.working_model:
        print(f"    working model: {result.working_model}")


def write_report(results: list["Result"], report_path: Path) -> str:
    """--report: the same diagnostics as the in-app report, as Markdown.

    The file lands in ``data/diagnostics/report.md`` (gitignored). It
    contains NO keys: the key column is the already-masked hint, provider
    messages are swept through ``redact`` (AIza…/gsk_…/sk-…/Bearer…/key=…),
    and nothing else in the report carries credential material.
    """
    from app.providers.outcomes import redact

    lines = [
        "# Provider diagnostics report",
        "",
        "Contains no keys. Provider messages are redacted; nothing below is a credential.",
        "",
    ]
    for result in results:
        spec = PROVIDER_SPECS[result.provider]
        from app.providers.model_registry import key_transport

        lines.append(f"## {result.provider} ({spec.host})")
        lines.append("")
        lines.append(f"- Key: {result.key_masked}")
        lines.append(f"- Key transport: {key_transport(result.provider)}")
        accepted = "unknown" if result.key_accepted is None else ("YES" if result.key_accepted else f"NO (HTTP {result.status})")
        lines.append(f"- Key accepted: {accepted}")
        lines.append(f"- Chat models listed: {len(result.models)}")
        if result.chosen:
            lines.append(f"- Would choose: {result.chosen} ({result.model_source})")
        lines.append(f"- Probe: {'OK' if result.probe_ok else 'FAILED' if result.probe_ok is not None else 'skipped'} — verdict {result.verdict}")
        if result.probe_detail:
            detail = " ".join(redact(result.probe_detail).split())
            lines.append(f"- Detail: {detail}")
        if result.walk:
            lines.append("")
            lines.append("| model | result | reason |")
            lines.append("|---|---|---|")
            for step in result.walk:
                reason = " ".join(redact(str(step.get("reason") or "")).split()).replace("|", "\\|")
                lines.append(
                    f"| {step['model']} | {step['outcome']} | {reason or '—'} |"
                )
        lines.append("")
    report = "\n".join(lines)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")
    return str(report_path)


def main() -> int:
    return asyncio.run(main_async(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
