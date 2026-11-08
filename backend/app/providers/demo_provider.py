"""Demo provider — a MOCK upstream that never makes a network call.

Why it exists: the first thing a new user sees should not be three red rows
saying "no API key". Demo mode lets the dashboard show routing, fallback,
latency and cost behaviour with **no key, no account, no spend**.

Honesty rules (this is the whole point):

* The provider is named ``mock`` and its model ``mock-demo-v1``, and every
  response carries :data:`DEMO_MARKER` so nothing can be mistaken for a real
  completion.
* Responses are clearly labelled in the text, so a screenshot is never
  ambiguous.
* :data:`DEMO_LABEL` / :func:`is_demo_provider` let the API, the logs and the
  benchmark reports exclude demo traffic: a benchmark that counted synthetic
  latencies would be a fabricated number.
* Simulated latency and an optional failure rate are configurable so the
  circuit breaker, chaos mode and fallback chain can be exercised for real.

The provider is registered only when ``DEMO_MODE_ENABLED`` is true, and it is
never selected while a real, configured provider is available (see
``providers.registry.routable_providers``).
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Optional

from app.config import settings
from app.providers.base import (
    ModelProvider,
    ProviderResponse,
    elapsed_ms,
    make_error,
    make_success,
)
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: Registry name. Deliberately not a real vendor name.
DEMO_PROVIDER_NAME = "mock"
#: Model name reported in responses and logs.
DEMO_MODEL_NAME = "mock-demo-v1"
#: Prepended to every synthetic completion so it cannot be mistaken for real.
DEMO_MARKER = "[DEMO — synthetic response, no model was called]"

#: Canned answers, picked by a cheap keyword heuristic. Short on purpose.
_CANNED: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("code", "python", "function", "bug", "regex", "sql", "typescript"),
        "Here is a synthetic code answer. A real provider would return a "
        "compilation-tested snippet here; this response exists to show the "
        "routing path, not to solve your problem.",
    ),
    (
        ("summarise", "summarize", "summary", "tldr"),
        "Synthetic summary: three bullet points would appear here, generated "
        "by whichever model the router selected.",
    ),
    (
        ("translate", "french", "spanish", "german"),
        "Synthetic translation output. The real router would send this prompt "
        "to a model with strong multilingual capability.",
    ),
    (
        ("why", "explain", "reason", "compare", "analyse", "analyze"),
        "Synthetic reasoning trace. The router ranks providers by capability "
        "for reasoning tasks, then falls back down the chain on failure.",
    ),
)

_DEFAULT_BODY = (
    "Synthetic completion. RouteLLM classified this prompt, ranked the "
    "available providers, and called one of them — in demo mode that provider "
    "is this mock, so no network request and no credits were used."
)


def is_demo_provider(provider_name: str) -> bool:
    """True for the mock provider — used to exclude demo traffic from reports."""
    return provider_name == DEMO_PROVIDER_NAME


def _canned_body(prompt: str) -> str:
    lowered = (prompt or "").lower()
    for keywords, body in _CANNED:
        if any(keyword in lowered for keyword in keywords):
            return body
    return _DEFAULT_BODY


class DemoProvider(ModelProvider):
    """A deterministic-ish fake: real timing shape, obviously fake content."""

    def __init__(
        self,
        latency_ms: Optional[int] = None,
        failure_rate: Optional[float] = None,
        seed: Optional[int] = None,
    ) -> None:
        self.provider_name = DEMO_PROVIDER_NAME
        self._latency_ms = latency_ms
        self._failure_rate = failure_rate
        self._random = random.Random(seed)
        self.calls = 0

    # -- configuration is read lazily so changing settings takes effect ------

    @property
    def latency_ms(self) -> int:
        if self._latency_ms is not None:
            return self._latency_ms
        return int(getattr(settings, "DEMO_LATENCY_MS", 350) or 0)

    @property
    def failure_rate(self) -> float:
        if self._failure_rate is not None:
            return float(self._failure_rate)
        return float(getattr(settings, "DEMO_FAILURE_RATE", 0.0) or 0.0)

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:  # noqa: ARG002
        """Return a synthetic completion after a realistic delay.

        Never raises: a provider contract violation would break the executor's
        fallback assumptions.
        """
        self.calls += 1
        start = time.perf_counter()

        # Jitter ±20% so the dashboard's latency chart is not a flat line.
        delay = max(0, self.latency_ms) * self._random.uniform(0.8, 1.2) / 1000.0
        if delay:
            await asyncio.sleep(delay)

        if self.failure_rate > 0 and self._random.random() < self.failure_rate:
            logger.info(
                "demo_provider_simulated_failure",
                provider=DEMO_PROVIDER_NAME,
                failure_rate=self.failure_rate,
            )
            return make_error(
                DEMO_PROVIDER_NAME,
                DEMO_MODEL_NAME,
                "demo_simulated_failure",
                elapsed_ms(start),
            )

        text = f"{DEMO_MARKER}\n\n{_canned_body(prompt)}"
        # ~4 characters per token is the usual rule of thumb; good enough for a
        # number that only ever appears in a labelled demo response.
        tokens = max(1, (len(prompt) + len(text)) // 4)
        return make_success(
            DEMO_PROVIDER_NAME,
            DEMO_MODEL_NAME,
            text,
            tokens,
            elapsed_ms(start),
        )
