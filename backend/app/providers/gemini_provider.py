import time

import httpx

from app.config import settings
from app.providers.base import ModelProvider, ProviderResponse, elapsed_ms, make_success
from app.providers.model_registry import PROVIDER_SPECS, resolve_model, spec_for
from app.providers.outcomes import error_for_exception, error_for_status

SPEC = PROVIDER_SPECS["gemini"]


class GeminiProvider(ModelProvider):
    """Google AI Studio (generativelanguage) v1beta generateContent.

    The model lives in the PATH, so the URL is rebuilt per request from the
    discovered model id. The previous implementation baked
    ``gemini-2.0-flash`` into a class constant, which meant the model could
    never be changed without editing code — and a lock in that constant became
    an unchangeable 404.
    """

    provider_name = "gemini"
    TIMEOUT = 15.0
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
        api_key = kwargs.get("provider_api_key") or settings.GEMINI_API_KEY
        model, _available, _source = await self.resolve_model(
            api_key,
            override=kwargs.get("model") or settings.GEMINI_MODEL or None,
            force_refresh=bool(kwargs.get("refresh_model")),
        )

        payload = {"contents": [{"parts": [{"text": prompt}]}]}
        generation_config = {}
        if kwargs.get("temperature") is not None:
            generation_config["temperature"] = kwargs["temperature"]
        if kwargs.get("max_tokens") is not None:
            generation_config["maxOutputTokens"] = kwargs["max_tokens"]
        if generation_config:
            payload["generationConfig"] = generation_config

        headers = {"Content-Type": "application/json"}
        if api_key:
            # Header auth keeps the provider secret out of the request URL.
            headers["x-goog-api-key"] = api_key

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
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            usage = data.get("usageMetadata") or {}
            return make_success(
                self.provider_name,
                model,
                text,
                usage.get("totalTokenCount", 0),
                latency_ms,
            )
        except Exception as exc:
            return error_for_exception(
                exc,
                provider=self.provider_name,
                model=model,
                url=url,
                latency_ms=elapsed_ms(start),
            )
