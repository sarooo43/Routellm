from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    DATABASE_URL: Optional[str] = None
    REDIS_URL: Optional[str] = None
    GROQ_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    DEEPSEEK_API_KEY: Optional[str] = None
    # Fernet key used to encrypt per-caller provider API keys (BYOK) at rest.
    # Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # When unset, provider-key writes and BYOK routing fail closed with 503.
    PROVIDER_KEY_ENCRYPTION_KEY: Optional[str] = None
    # Comma-separated browser origins allowed to call this backend cross-origin,
    # e.g. CORS_ORIGINS=http://localhost:5173,https://routellm.example.workers.dev
    # (the Cloudflare Worker proxies same-origin server-to-server, so this only
    # matters for clients that set VITE_API_BASE_URL directly at the backend).
    CORS_ORIGINS: str = ""
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    ROUTE_RATE_LIMIT: str = "30/minute"
    # DEV/DEMO ONLY: fault-injection endpoints + executor fake failures.
    # Never enabled by default; must be explicitly set via .env (docker/env).
    # See core/chaos.py — a Chaos Monkey-style demo control for the dashboard.
    CHAOS_MODE_ENABLED: bool = False
    # EXPERIMENTAL: ML-assisted routing (learned capability scores blended
    # with the rule-based table, 60/40). Off until the router model is
    # trained (scripts/train_router_model.py) AND validated. Requires the
    # model file backend/app/ml_models/router_classifier.pkl to exist.
    ENABLE_ML_ROUTING: bool = False
    # Port to bind when the app is started as a script (hf_app.py). Hugging
    # Face Spaces require 7860; docker-compose/uvicorn pass --port explicitly.
    PORT: int = 7860
    # Set to "1"/"true" to force the Hugging Face filesystem rules (no writes
    # inside the app dir) when testing a read-only deployment elsewhere.
    ROUTELLM_READ_ONLY_APP_DIR: str = ""
    # Optional explicit runtime directory override (logs/scratch files).
    ROUTELLM_RUNTIME_DIR: str = ""
    # Per-component budget for /health probes. Kept short so a sleeping
    # Space's health check cannot hang a load balancer.
    HEALTH_PROBE_TIMEOUT_S: float = 2.0


settings = Settings()


class _CorsOrigins:
    """Module-level helper so tests can inspect the parsed allowlist."""

    @staticmethod
    def parse(raw: str) -> list[str]:
        origins = [o.strip().rstrip("/") for o in (raw or "").split(",") if o.strip()]
        return origins


def normalize_redis_url(raw: Optional[str]) -> Optional[str]:
    """Return a Redis DSN that the async client accepts, or None when unset.

    ``rediss://`` (TLS, what Upstash hands out) is passed through untouched —
    ``redis.asyncio.from_url`` already selects the TLS connection class from
    the scheme. The only rewrite is the legacy/managed-provider habit of using
    ``redis://`` against a TLS-only host, which is left alone *on purpose*:
    silently upgrading the scheme would turn a clear connection error into a
    confusing TLS handshake failure. See docs/DEPLOY.md troubleshooting.
    """
    if not raw:
        return None
    url = raw.strip()
    if not url:
        return None
    if url.startswith(("redis://", "rediss://", "unix://")):
        return url
    raise ValueError(
        "REDIS_URL must start with redis://, rediss:// or unix:// "
        f"(got {url.split(':', 1)[0]!r})"
    )


def redis_uses_tls(raw: Optional[str]) -> bool:
    """True for rediss:// (TLS) DSNs — used by the startup log."""
    return bool(raw) and str(raw).strip().lower().startswith("rediss://")


def cors_origin_list(settings_obj: Optional[Settings] = None) -> list[str]:
    """Browser-origin allowlist for CORSMiddleware. Falls back to the Vite
    dev origins when CORS_ORIGINS is unset so local development needs zero
    configuration; production origins must be set explicitly."""
    s = settings_obj or settings
    origins = _CorsOrigins.parse(s.CORS_ORIGINS)
    if origins:
        return origins
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ]
