"""Dashboard demo server — real FastAPI app + stubbed providers.

Runs exactly the production backend, with PROVIDER_REGISTRY entries replaced
by stubs (no real API keys needed) and a local SQLite file for persistence.
TaskLogs persist across restarts so the Overview feed has history. Fire
POST /api/v1/route requests in another terminal and watch the Overview
live feed / KPI cards update. Delete demo.db to reset.

Usage (from backend/):  & .venv\\Scripts\\python.exe scripts\\demo_server.py
"""

import asyncio
import os
import sys
from pathlib import Path

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./demo.db"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # make backend/ importable from scripts/

import uvicorn  # noqa: E402  (env var must be set before app imports)

from app.db.session import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.core.scorer import COST_PER_1K_TOKENS  # noqa: F401,E402  (costs are real)
from app.providers.registry import PROVIDER_REGISTRY  # noqa: E402
from app.providers.base import ProviderResponse  # noqa: E402
from app.providers.base import ModelProvider  # noqa: E402
import random  # noqa: E402


class DemoProvider(ModelProvider):
    """Stub that succeeds for groq/deepseek and fails ~30% for gemini."""

    def __init__(self, provider_name: str, failure_rate: float = 0.0):
        self.provider_name = provider_name
        self.failure_rate = failure_rate

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        latency = round(random.uniform(35.0, 180.0), 1)
        if random.random() < self.failure_rate:
            return ProviderResponse(
                success=False,
                error=random.choice(["timeout", "rate_limit_exceeded", "provider_server_error"]),
                latency_ms=latency,
                tokens_used=0,
                provider_name=self.provider_name,
                model_name="demo-model",
            )
        return ProviderResponse(
            success=True,
            text=f"[demo:{self.provider_name}] Here is my response to: {prompt[:60]!r}",
            tokens_used=random.randint(20, 400),
            latency_ms=latency,
            provider_name=self.provider_name,
            model_name="demo-model",
        )


PROVIDER_REGISTRY["groq"] = DemoProvider("groq", failure_rate=0.0)
PROVIDER_REGISTRY["gemini"] = DemoProvider("gemini", failure_rate=0.4)
PROVIDER_REGISTRY["deepseek"] = DemoProvider("deepseek", failure_rate=0.15)


async def main() -> None:
    await init_db()
    config = uvicorn.Config(app, host="127.0.0.1", port=8000, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
