#!/usr/bin/env python
"""check_providers.py — provider connectivity + model explorer report.

Boots like the app does, then for every configured provider prints:
  - the masked key hint (last 4 characters ONLY — never the key),
  - the discovered model list sorted like the Model Explorer
    (free first, then newest; version parsed as a NUMBER),
  - with ``--test``: the same candidate walk real routing uses, including
    each skipped model and the reason it was skipped.

Run it with the project venv:
    Linux/macOS:  .venv/bin/python backend/scripts/check_providers.py
    Windows (pwsh): backend\\.venv\\Scripts\\python.exe backend\\scripts\\check_providers.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent

# The script may be launched from any working directory; make `app` importable
# regardless (harmless when the cwd already provides it).
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

#: The friendly message mandated for a bare system-Python run without a venv.
VENV_MESSAGE = (
    "Run with the project venv: .venv/bin/python backend/scripts/check_providers.py, "
    "or run python run.py once to create it."
)


def find_project_venv() -> Path | None:
    """Locate the project's virtualenv python, Linux/macOS or Windows."""
    candidates = [
        BACKEND_DIR / ".venv" / "bin" / "python",
        BACKEND_DIR / ".venv" / "Scripts" / "python.exe",
        BACKEND_DIR / ".venv" / "Scripts" / "python",
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

import app.config  # noqa: E402  (safe: bootstrap above re-exec'd if needed)
from app.core import model_discovery as md  # noqa: E402
from app.providers.registry import PROVIDER_REGISTRY  # noqa: E402


def mask_key(key: str | None) -> str:
    """Show only the last 4 characters — never the key itself."""
    if not key:
        return "(not configured)"
    return "****" + key[-4:]


async def report(test_mode: bool) -> int:
    settings = app.config.settings
    exit_code = 0

    for provider_name in PROVIDER_REGISTRY:
        env_key_name = md.PROVIDER_ENV_KEY.get(provider_name)
        key = getattr(settings, env_key_name, None) if env_key_name else None
        print(f"\n=== {provider_name} ===")
        print(f"key: {mask_key(key)}")
        if not key:
            print("status: skipped (no operator key configured)")
            continue

        models, reason = await md.discover_models(provider_name, key)
        if not models:
            print(f"discovery: FAILED — {reason}")
            exit_code = max(exit_code, 1)
            continue

        visible, hidden = md.sort_models_for_provider(
            models, provider_name, sort="tier", tier_filter="all"
        )
        print(f"discovery: {len(visible)} chat models ({hidden} non-chat hidden)")
        for model in visible:
            version = (
                ".".join(str(part) for part in model.version)
                if model.version
                else "-"
            )
            status = (
                md.get_cached_status(key, provider_name, model.id) or "untested"
            )
            tier = md.estimated_tier(provider_name, model.id)
            print(f"  - {model.id:<44} tier={tier:<8} version={version:<8} status={status}")

        if not test_mode:
            continue

        print("candidate walk (--test):")
        walk = await md.walk_candidates(
            provider_name,
            "ping",
            {"max_tokens": 1},
            key,
            first_model=None,
            max_attempts=3,
        )
        for step in walk.steps:
            if step.skipped:
                print(f"  SKIP {step.model}: {step.reason}")
            elif step.success:
                print(f"  OK   {step.model} ({step.latency_ms:.0f} ms)")
            else:
                print(f"  FAIL {step.model}: {step.reason}")
        if walk.response.success:
            print(f"working model: {walk.model_used}")
        else:
            print(f"no working model found — {walk.response.error}")
            exit_code = 2

    return exit_code


def main() -> None:
    test_mode = "--test" in sys.argv[1:]
    try:
        exit_code = asyncio.run(report(test_mode))
    except KeyboardInterrupt:
        exit_code = 130
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
