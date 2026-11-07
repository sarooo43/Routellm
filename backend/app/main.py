from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.catalog_routes import router as catalog_router
from app.api.keys_routes import router as keys_router
from app.api.openai_compat_routes import router as openai_compat_router
from app.api.provider_keys_routes import router as provider_keys_router
from app.api.routes import health_payload, limiter, router
from app.config import cors_origin_list, settings
from app.db.session import DatabaseUnavailableError
from app.utils.logger import get_logger
from app.utils.masking import mask_secrets


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = get_logger(__name__)
    logger.info("RouteLLM backend starting...")
    await _log_startup_config(logger)

    schema_retry_task = None
    if settings.DATABASE_URL:
        # Create tables if they don't exist yet (first boot on a fresh host —
        # e.g. a brand-new Neon database). NON-FATAL on an unreachable DB:
        # a managed cloud Postgres may still be waking up when the Space
        # starts, and crash-looping the whole API because of that is worse
        # than serving degraded. init_db() is retried in the background
        # until it succeeds, so the schema appears without a restart.
        from app.db.session import init_db, masked_database_url

        try:
            await init_db()
            logger.info("database_ready")
        except Exception as e:
            logger.warning(
                "database_unreachable_at_boot_will_retry",
                error_type=type(e).__name__,
                url=masked_database_url(settings.DATABASE_URL),
                error_masked=mask_secrets(str(e)),
            )

            async def _create_schema_when_reachable():
                attempt = 0
                while True:
                    attempt += 1
                    wait_s = min(15 * attempt, 60)
                    await asyncio.sleep(wait_s)
                    try:
                        await init_db()
                        logger.info("database_ready_after_retry", attempt=attempt)
                        return
                    except Exception as retry_exc:
                        logger.warning(
                            "database_init_retry_failed",
                            attempt=attempt,
                            error_masked=mask_secrets(str(retry_exc)),
                        )

            schema_retry_task = asyncio.create_task(_create_schema_when_reachable())

    # EXPERIMENTAL ML routing: eagerly warm the model at startup ONLY when
    # the flag is on, so the expensive sentence-transformer load never adds
    # latency to a live request. If the model file doesn't exist this logs
    # and the system quietly stays rule-based.
    if settings.ENABLE_ML_ROUTING:
        from app.core.ml_classifier import get_router_classifier

        classifier = get_router_classifier()
        logger.info(
            "ml_routing_startup_probe",
            flag_enabled=True,
            model_usable=classifier.is_loaded,
        )
    try:
        yield
    finally:
        logger.info("RouteLLM backend shutting down...")
        if schema_retry_task is not None:
            schema_retry_task.cancel()


async def _log_startup_config(logger) -> None:
    """C7: one structured line describing the deployment. Credentials are
    NEVER included (URLs masked, provider keys reduced to on/off, hosts to
    names without credentials)."""
    import os
    from urllib.parse import urlparse

    from app.db.session import describe_database_driver

    rediss = bool(settings.REDIS_URL) and settings.REDIS_URL.startswith("rediss://")
    redis_state = (
        "tls" if rediss else ("plain" if settings.REDIS_URL else "not_configured")
    )
    server_key_providers = [
        name
        for name, env_name in (
            ("groq", "GROQ_API_KEY"),
            ("gemini", "GEMINI_API_KEY"),
            ("deepseek", "DEEPSEEK_API_KEY"),
        )
        if getattr(settings, env_name)
    ]
    redis_host = (
        urlparse(settings.REDIS_URL).hostname if settings.REDIS_URL else None
    )
    logger.info(
        "startup_config",
        environment=settings.ENVIRONMENT,
        cors_origins=cors_origin_list(),
        database=describe_database_driver(),
        redis=(f"{redis_state} host={redis_host}" if redis_host else redis_state),
        server_key_providers=server_key_providers,
        chaos_mode_enabled=settings.CHAOS_MODE_ENABLED,
        ml_routing=("flag_on" if settings.ENABLE_ML_ROUTING else "off"),
        on_huggingface=bool(os.environ.get("SPACE_ID")),
        hf_space=os.environ.get("SPACE_ID"),
    )



app = FastAPI(title="RouteLLM API", version="0.1.0", lifespan=lifespan)

logger = get_logger(__name__)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(DatabaseUnavailableError)
async def _database_unavailable_handler(request, exc):
    """Clean 503 for a missing/unreachable Postgres — never a raw 500."""
    logger.error(
        "database_unavailable_503", path=request.url.path, error=str(exc)
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
app.include_router(openai_compat_router)


@app.get("/health")
async def health():
    payload, status_code = await health_payload()
    return JSONResponse(status_code=status_code, content=payload)


@app.get("/", include_in_schema=False)
async def root():
    """Cheap service landing (kept out of /docs). The dashboard lives on its
    own Worker domain; /docs, /health, /ui and the API prefixes carry the
    rest. Never touch dependencies here so this always answers instantly."""
    return {
        "service": "routellm-backend",
        "version": "0.1.0",
        "docs": "/docs",
        "health": "/health",
        "ui": "/ui",
        "api": "/api/v1",
    }

