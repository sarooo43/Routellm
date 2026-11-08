"""First-run status for the onboarding checklist.

One public endpoint answers "is this installation ready, and what is missing?".
The dashboard renders it as a checklist; nothing here is an account system and
nothing is secret — the payload reports *presence*, never values.

``GET /api/v1/setup/status``::

    {
      "local_mode": true,
      "require_api_key": false,
      "providers": [
        {"name": "groq", "display": "Groq", "key_source": "env",
         "uses_own_key": true, "demo": false, "console_url": "..."}
      ],
      "any_provider_configured": true,
      "any_provider_usable": true,
      "demo_mode_enabled": true,
      "demo_available": false,
      "demo_provider_name": "mock",
      "chaos_mode_enabled": true,
      "data_dir": "./data",
      "database_driver": "sqlite",
      "preferred_provider": "groq",
      "preferred_console_url": "https://console.groq.com/keys",
      "steps": [{"id": "provider_key", "done": false, "..."}]
    }

Scope of "configured": the caller's own stored provider keys (BYOK) plus, in
local mode, operator keys from the environment. That mirrors routing exactly:
BYOK wins, environment keys are the fallback.
"""

from typing import Optional

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_key
from app.config import settings
from app.core.provider_key_manager import list_provider_keys
from app.db.session import effective_database_url, get_session
from app.local import data_dir
from app.models.api_key import ApiKeyDB
from app.providers.registry import (
    PROVIDER_REGISTRY,
    demo_provider_available,
    server_key_configured_providers,
)

router = APIRouter(prefix="/setup", tags=["Setup"])

#: Presentation metadata for the three real providers. Kept server-side so the
#: dashboard and the README cannot drift apart.
PROVIDER_META: dict[str, dict[str, str]] = {
    "groq": {
        "display": "Groq",
        "console_url": "https://console.groq.com/keys",
        "free_note": "Free tier, no credit card. Fastest option.",
    },
    "gemini": {
        "display": "Google AI Studio",
        "console_url": "https://aistudio.google.com/app/apikey",
        "free_note": "Free tier with a generous daily quota.",
    },
    "deepseek": {
        "display": "DeepSeek",
        "console_url": "https://platform.deepseek.com/api_keys",
        "free_note": "Low-cost; strong at code.",
    },
}

#: Order shown in the UI: fastest/cheapest first.
PREFERRED_ORDER = ("groq", "gemini", "deepseek")

STEP_DEFINITIONS = {
    "provider_key": "Add a free provider key",
    "test_prompt": "Send a test prompt",
    "try_failover": "Watch the router fail over",
}


def _database_driver() -> Optional[str]:
    """Driver of the effective database ('sqlite' locally), never the DSN."""
    from app.db.url import normalize_database_url

    url = effective_database_url()
    if not url:
        return None
    try:
        return normalize_database_url(url).driver
    except Exception:  # pragma: no cover - defensive
        return "invalid"


def _display_path(candidate) -> str:
    """Repo-relative path when possible, so logs/screenshots stay portable."""
    from pathlib import Path

    try:
        repo_root = Path(__file__).resolve().parents[3]
        return f"./{candidate.relative_to(repo_root)}"
    except (ValueError, OSError):  # pragma: no cover - external data dir
        return str(candidate)


@router.get("/status")
async def setup_status(
    api_key: ApiKeyDB = Depends(require_api_key),
    # Same session as auth: the checklist only runs once auth has already
    # needed the database, so a second connection would add nothing.
    db_session: AsyncSession = Depends(get_session),
) -> dict:
    """Presence-only readiness snapshot used by the onboarding checklist."""
    stored = {row.provider_name for row in await list_provider_keys(db_session, api_key.id)}

    env_names = set(server_key_configured_providers())
    local_mode = not settings.REQUIRE_API_KEY

    providers = []
    for name in PROVIDER_REGISTRY:
        meta = PROVIDER_META.get(name)
        if meta is None:  # the demo provider is reported separately
            continue
        from_env = name in env_names
        by_key = name in stored
        providers.append(
            {
                "name": name,
                "display": meta["display"],
                "console_url": meta["console_url"],
                "free_note": meta["free_note"],
                "configured": by_key or from_env,
                # Routing precedence: the caller's own key wins, then env.
                "key_source": "byok" if by_key else ("env" if from_env else None),
                "usable": bool(by_key or from_env),
            }
        )

    any_configured = any(p["configured"] for p in providers)
    demo_available = demo_provider_available() and not any_configured

    preferred = next(
        (p for p in providers if not p["configured"]),
        None,
    )
    if preferred is None and providers:
        preferred = providers[0]

    steps = [
        {
            "id": "provider_key",
            "title": STEP_DEFINITIONS["provider_key"],
            "done": any_configured,
            "detail": (
                "Routing already works through your saved provider keys."
                if any_configured
                else "Demo mode is available now; a real key routes to a real model."
            ),
        },
        {
            "id": "test_prompt",
            "title": STEP_DEFINITIONS["test_prompt"],
            # Presence of a routing decision is checked client-side against the
            # task log; the server only knows whether it COULD work.
            "done": False,
            "detail": "Open the Explorer page and send any prompt.",
        },
        {
            "id": "try_failover",
            "title": STEP_DEFINITIONS["try_failover"],
            "done": False,
            "detail": (
                "Use 'Simulate outage' — the router retries the next provider "
                "and records the attempt chain."
                if settings.CHAOS_MODE_ENABLED
                else "Enable CHAOS_MODE_ENABLED to try simulated outages."
            ),
        },
    ]

    return {
        "local_mode": local_mode,
        "require_api_key": settings.REQUIRE_API_KEY,
        "providers": providers,
        "any_provider_configured": any_configured,
        "any_provider_usable": any(p["usable"] for p in providers),
        "demo_mode_enabled": settings.DEMO_MODE_ENABLED,
        "demo_available": demo_available,
        "demo_provider_name": "mock",
        "chaos_mode_enabled": settings.CHAOS_MODE_ENABLED,
        "data_dir": _display_path(data_dir(create=False)),
        "database_driver": _database_driver(),
        "preferred_provider": preferred["name"] if preferred else None,
        "preferred_console_url": preferred["console_url"] if preferred else None,
        "steps": steps,
    }
