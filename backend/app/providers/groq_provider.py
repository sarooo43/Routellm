import time

import httpx

from app.config import settings
from app.providers.base import ModelProvider, ProviderResponse, elapsed_ms, make_success
from app.providers.model_registry import PROVIDER_SPECS, resolve_model, spec_for
from app.providers.outcomes import error_for_exception, error_for_status

SPEC = PROVIDER_SPECS["groq"]


class GroqProvider(ModelProvider):
    """Groq: OpenAI-compatible chat completions under /openai/v1.

    The model is DISCOVERED, not assumed. A hardcoded model is what turned a
    retired model name into a 404 that was then reported to the user as a bad
    API key.
    """

    provider_name = "groq"
    TIMEOUT = 15.0

    #: Kept for callers that read it; the live value comes from discovery.
    DEFAULT_MODEL = SPEC.default_model

    #: Value (not a property): other code and tests read this as an attribute.
    #: The live URL is built per request from the discovered model.
    API_URL = SPEC.chat_url(SPEC.default_model)

    async def resolve_model(self, api_key, override=None, force_refresh=False):
        spec = spec_for(self.provider_name)
        return await resolve_model(
            self.provider_name, api_key, override=override, force_refresh=force_refresh
        )

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        api_key = kwargs.get("provider_api_key") or settings.GROQ_API_KEY
        model, _available, _source = await self.resolve_model(
            api_key,
            override=kwargs.get("model") or settings.GROQ_MODEL or None,
            force_refresh=bool(kwargs.get("refresh_model")),
        )

        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get("temperature", 0.7),
        }
        if kwargs.get("max_tokens") is not None:
            payload["max_tokens"] = kwargs["max_tokens"]

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        url = SPEC.chat_url(model)
        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                response = await client.post(url, json=payload, headers=headers)
            latency_ms = elapsed_ms(start)

            if response.status_code != 200:
                return error_for_status(
                    response,
                    provider=self.provider_name,
                    model=model,
                    url=url,
                    latency_ms=latency_ms,
                )

            data = response.json()
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            return make_success(
                self.provider_name, model, text, usage.get("total_tokens", 0), latency_ms
            )
        except Exception as exc:
            return error_for_exception(
                exc,
                provider=self.provider_name,
                model=model,
                url=url,
                latency_ms=elapsed_ms(start),
            )
