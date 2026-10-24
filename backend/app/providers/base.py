import time
from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel


class ProviderResponse(BaseModel):
    success: bool
    text: str = ""
    tokens_used: int = 0
    latency_ms: float = 0.0
    error: Optional[str] = None
    provider_name: str
    model_name: str


class ModelProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        """Send prompt to the underlying LLM API and return a structured response.
        MUST catch all exceptions internally and never raise."""
        ...


def elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def make_error(
    provider_name: str, model_name: str, error: str, latency_ms: float
) -> ProviderResponse:
    return ProviderResponse(
        success=False,
        text="",
        tokens_used=0,
        latency_ms=latency_ms,
        error=error,
        provider_name=provider_name,
        model_name=model_name,
    )


def make_success(
    provider_name: str,
    model_name: str,
    text: str,
    tokens_used,
    latency_ms: float,
) -> ProviderResponse:
    if not text or not text.strip():
        return make_error(provider_name, model_name, "empty_response", latency_ms)
    return ProviderResponse(
        success=True,
        text=text,
        tokens_used=int(tokens_used) if tokens_used else 0,
        latency_ms=latency_ms,
        error=None,
        provider_name=provider_name,
        model_name=model_name,
    )
