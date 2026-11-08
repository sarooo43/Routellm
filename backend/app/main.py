"""FastAPI application factory and process lifecycle hooks.

Startup is intentionally **non-fatal**: a host that boots before Postgres or
Redis is reachable (or a laptop whose SQLite file is briefly locked) must still
serve ``/health``, ``/docs`` and the static catalog. Any unreachable dependency
is logged (masked) and reported by ``/health`` instead of aborting the process.

LOCAL-FIRST DEFAULT: with no configuration at all the app uses SQLite at
``<data dir>/routellm.db`` and an in-process store in place of Redis, so
``python run.py`` is the whole setup. Postgres/Redis are opt-in via
``DATABASE_URL``/``REDIS_URL``.
"""

import os
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import SQLAlchemyError

from app.api.catalog_routes import router as catalog_router
from app.api.keys_routes import router as keys_router
from app.api.openai_compat_routes import router as openai_compat_router
from app.api.provider_keys_routes import router as provider_keys_router
from app.api.setup_routes import router as setup_router
from app.api.routes import health_payload, limiter, router
from app.config import (
    cors_origin_list,
    redis_uses_tls,
    settings,
)
from app.db.session import (
    DatabaseUnavailableError,
    effective_database_url,
    ensure_schema,
)
from app.db.url import mask_database_url, normalize_database_url
from app.identity import APP_NAME, APP_VERSION as IDENTITY_VERSION, PROJECT_URL, UPSTREAM_DISCLAIMER
from app.frontend import (
    frontend_status,
    install_root_status_page,
    mount_frontend,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Version reported by /health and the OpenAPI document. Sourced from
#: app.identity so the API, the dashboard and the docs cannot disagree.
APP_VERSION = IDENTITY_VERSION

#: Set once the process is up; /health reports the delta as uptime_s.
_STARTED_AT = time.monotonic()


def app_uptime_seconds() -> float:
    """Seconds since this process started serving (monotonic, wall-clock safe)."""
    return round(time.monotonic() - _STARTED_AT, 3)


def _provider_keys_configured() -> list[str]:
    """Names of providers with an operator (SERVER) key — never the values."""
    candidates = {
        "groq": "GROQ_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
    }
    return [name for name, env in candidates.items() if os.environ.get(env, "").strip()]


def _database_driver() -> str | None:
    """Normalized driver name for the startup log (never the DSN).

    Reports the driver of the *effective* URL, so a local run says ``sqlite``
    rather than nothing at all.
    """
    url = effective_database_url()
    if not url:
        return None
    try:
        return normalize_database_url(url).driver
    except Exception:
        return "invalid"


def _cors_origins_for_log() -> list[str]:
    """Allowlisted origins; origins are public by definition, safe to log."""
    return cors_origin_list()


def log_startup_diagnostics(mode: str | None = None) -> None:
    """One structured line describing the effective configuration.

    Everything secret is reduced to a presence flag or a masked DSN — see
    ``app.db.url.mask_database_url`` and ``app.utils.logger.redact_env``.
    """
    from app.runtime import running_on_huggingface

    logger.info(
        "startup_configuration",
        environment=settings.ENVIRONMENT,
        version=APP_VERSION,
        app_mode=mode,
        on_huggingface=running_on_huggingface(),
        cors_origins=_cors_origins_for_log(),
        database={
            "configured": bool(settings.DATABASE_URL),
            "local_default": not bool(settings.DATABASE_URL),
            "dsn": mask_database_url(effective_database_url() or ""),
            "driver": _database_driver(),
        },
        require_api_key=settings.REQUIRE_API_KEY,
        redis={
            "configured": bool(settings.REDIS_URL),
            "tls": redis_uses_tls(settings.REDIS_URL),
            "fallback": None if settings.REDIS_URL else "in-memory fakeredis",
        },
        server_provider_keys=_provider_keys_configured(),
        byok_encryption_configured=bool(settings.PROVIDER_KEY_ENCRYPTION_KEY),
        chaos_mode=settings.CHAOS_MODE_ENABLED,
        ml_routing=settings.ENABLE_ML_ROUTING,
        data_dir=str(_data_dir_for_log()),
    )


def _data_dir_for_log() -> str:
    """Where local state lives (a path, never a secret)."""
    from app.local import data_dir

    try:
        return str(data_dir(create=False))
    except OSError:  # pragma: no cover - unreadable host
        return "<unavailable>"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Register the app's startup/shutdown work.

    Importable on its own so entrypoints that replace the app's lifespan
    (Gradio's ``mount_gradio_app`` does exactly that) can re-apply it without
    duplicating the logic.
    """
    # Non-fatal either way: a brand-new or sleeping database is a normal
    # condition. ensure_schema() logs and returns False; the session
    # dependency retries on the next request. With no DATABASE_URL this runs
    # against the SQLite file in the data dir, which is what makes a fresh
    # clone work with zero manual steps.
    if not settings.DATABASE_URL:
        # The one place this is announced: consumers resolve the URL lazily and
        # would otherwise repeat it once each.
        logger.info(
            "database_defaulted_to_sqlite",
            detail="DATABASE_URL unset — using the local SQLite file under the data directory.",
            data_dir=_data_dir_for_log(),
        )
    await ensure_schema()

    if not os.environ.get("REDIS_URL", "").strip():
        # Local-first: this is the expected state, not a misconfiguration.
        logger.info(
            "redis_in_process_store",
            detail="REDIS_URL unset — using the in-process store: no "
            "cross-process state, state resets when the process exits.",
        )

    status = frontend_status(app)
    if status["mounted"]:
        logger.info(
            "frontend_served_from_backend",
            directory=status["directory"],
            root="/",
            spa_fallback=True,
            assets="/assets",
        )
    elif status["reason"] == "serving_disabled":
        logger.info(
            "frontend_serving_disabled",
            detail="SERVE_FRONTEND=false — the dashboard is served separately.",
        )
    else:
        logger.info(
            "frontend_not_built",
            detail="frontend/dist not found — API only. Build it with "
            "`cd frontend && npm ci && npm run build`, or run `python run.py --dev`.",
        )

    if settings.ENABLE_ML_ROUTING:
        from app.core.ml_classifier import get_router_classifier

        classifier = get_router_classifier()
        logger.info(
            "ml_routing_startup_probe",
            flag_enabled=True,
            model_usable=classifier.is_loaded,
        )
    yield
    logger.info("backend_shutdown", app=APP_NAME)


app = FastAPI(
    title=f"{APP_NAME} API",
    version=APP_VERSION,
    description=(
        "Intelligent LLM routing gateway: classify → rank → execute with "
        "automatic fallback, health tracking and cost analytics.\n\n"
        f"{UPSTREAM_DISCLAIMER}\n\n"
        f"Source: {PROJECT_URL}"
    ),
    lifespan=lifespan,
    # Root is claimed by a redirect to the status page (see below), so the
    # auto-generated Swagger UI stays available at /docs.
    docs_url="/docs",
    redoc_url=None,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(DatabaseUnavailableError)
async def _database_unavailable_handler(request: Request, exc: Exception):
    """Clean 503 for a missing/unreachable Postgres — never a raw 500."""
    logger.error(
        "database_unavailable_503",
        path=request.url.path,
        error=str(exc),
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unavailable"},
    )


@app.exception_handler(SQLAlchemyError)
async def _sqlalchemy_error_handler(request: Request, exc: Exception):
    """Any driver-level failure (pooler closed, auth, DNS) is an outage.

    Mapping SQLAlchemy errors here means a handler that forgot its own
    try/except still answers with the documented 503 envelope instead of a
    bare 500 traceback.
    """
    logger.error(
        "database_error_503",
        path=request.url.path,
        database=mask_database_url(settings.DATABASE_URL) if settings.DATABASE_URL else None,
        error=str(exc).strip()[:400],
    )
    return JSONResponse(
        status_code=503,
        content={"detail": "Database temporarily unavailable"},
    )


app.add_middleware(
    CORSMiddleware,
    # Explicit browser-origin allowlist (comma-separated CORS_ORIGINS env, see
    # config.py). Wildcard "*" with credentials never works; and the deployed
    # dashboard calls same-origin through the Cloudflare Worker proxy, so this
    # only gates clients that set VITE_API_BASE_URL directly at the backend.
    allow_origins=cors_origin_list(),
    allow_methods=["*"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
    allow_credentials=False,
    expose_headers=["X-RateLimit-Remaining", "Retry-After"],
)

app.include_router(router, prefix="/api/v1")
app.include_router(catalog_router, prefix="/api/v1")
app.include_router(keys_router, prefix="/api/v1")
app.include_router(provider_keys_router, prefix="/api/v1")
app.include_router(setup_router, prefix="/api/v1")
app.include_router(openai_compat_router)


@app.get("/health", tags=["Health"])
async def health():
    """Public liveness/readiness probe.

    Always answers 200 with component states, so a Space's health check can
    distinguish "container alive but dependencies down" (degraded) from "no
    answer at all" (container dead). Probes are individually timed out.
    """
    payload, status_code = await health_payload()
    return JSONResponse(status_code=status_code, content=payload)


# --------------------------------------------------------------------------
# Dashboard (must come LAST: routes match in registration order)
#
# Local-first serves the built SPA from this process so the app is one URL.
# The catch-all only exists when frontend/dist is actually present; otherwise
# / renders a small status page explaining how to build it, and the API paths
# above are untouched either way.
# --------------------------------------------------------------------------
if mount_frontend(app) is None:
    install_root_status_page(app)
