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


settings = Settings()
