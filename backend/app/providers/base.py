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
    #: Machine-readable failure family ("auth_failed", "model_not_found",
    #: "network_error", ...). Callers decide whether to open the circuit
    #: breaker from THIS, not by pattern-matching the human sentence — the
    #: sentence is prose and prose changes.
    outcome: Optional[str] = None
    #: Provider-supplied Retry-After in SECONDS (from the 429 header), when
    #: present. The model-status cache uses it as the rate_limited TTL.
    retry_after: Optional[float] = None
    #: T4: the provider's own machine-readable error identity, parsed from
    #: the JSON body — Google's ``error.status`` (e.g. PERMISSION_DENIED)
    #: and ``error.reason`` when present. Surfaced by diagnostics and the
    #: explorer so a 403 is distinguishable from a 401 without prose.
    provider_status: Optional[str] = None
    provider_reason: Optional[str] = None


class ModelProvider(ABC):
    @abstractmethod
    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        """Send prompt to the underlying LLM API and return a structured response.
        MUST catch all exceptions internally and never raise."""
        ...


def elapsed_ms(start: float) -> float:
    return (time.perf_counter() - start) * 1000.0


def make_error(
    provider_name: str,
    model_name: str,
    error: str,
    latency_ms: float,
    outcome: Optional[str] = None,
    retry_after: Optional[float] = None,
    provider_status: Optional[str] = None,
    provider_reason: Optional[str] = None,
) -> ProviderResponse:
    return ProviderResponse(
        success=False,
        text="",
        tokens_used=0,
        latency_ms=latency_ms,
        error=error,
        provider_name=provider_name,
        model_name=model_name,
        outcome=outcome,
        retry_after=retry_after,
        provider_status=provider_status,
        provider_reason=provider_reason,
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
