from contextlib import asynccontextmanager

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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = get_logger(__name__)
    logger.info("RouteLLM backend starting...")
    if settings.DATABASE_URL:
        # Create tables if they don't exist yet (docker-compose first boot).
        from app.db.session import init_db

        try:
            await init_db()
            logger.info("database_ready")
        except Exception as e:
            logger.error("database_init_failed", error=str(e))
            raise

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
    yield
    logger.info("RouteLLM backend shutting down...")


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
