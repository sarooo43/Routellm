from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.routes import health_payload, limiter, router
from app.config import settings
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
    yield
    logger.info("RouteLLM backend shutting down...")


app = FastAPI(title="RouteLLM API", version="0.1.0", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.get("/health")
async def health():
    payload, status_code = await health_payload()
    return JSONResponse(status_code=status_code, content=payload)
