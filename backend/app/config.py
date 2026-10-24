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
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    ROUTE_RATE_LIMIT: str = "30/minute"


settings = Settings()
