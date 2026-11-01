import time

import httpx

from app.config import settings
from app.providers.base import (
    ModelProvider,
    ProviderResponse,
    elapsed_ms,
    make_error,
    make_success,
)


class GeminiProvider(ModelProvider):
    provider_name = "gemini"
    DEFAULT_MODEL = "gemini-2.0-flash"
    API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
    TIMEOUT = 15.0

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        model = kwargs.get("model", self.DEFAULT_MODEL)
        api_key = kwargs.get("provider_api_key") or settings.GEMINI_API_KEY
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

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                response = await client.post(self.API_URL, json=payload, headers=headers)
            latency_ms = elapsed_ms(start)

            if response.status_code == 429:
                return make_error(
                    self.provider_name, model, "rate_limit_exceeded", latency_ms
                )
            if response.status_code in (401, 403):
                return make_error(self.provider_name, model, "auth_failed", latency_ms)
            if response.status_code >= 500:
                return make_error(
                    self.provider_name, model, "provider_server_error", latency_ms
                )
            if response.status_code != 200:
                return make_error(
                    self.provider_name,
                    model,
                    f"unexpected_error: HTTP {response.status_code}",
                    latency_ms,
                )

            data = response.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            usage = data.get("usageMetadata") or {}
            tokens_used = usage.get("totalTokenCount", 0)
            return make_success(
                self.provider_name, model, text, tokens_used, latency_ms
            )
        except httpx.TimeoutException:
            return make_error(self.provider_name, model, "timeout", elapsed_ms(start))
        except Exception as e:
            return make_error(
                self.provider_name,
                model,
                f"unexpected_error: {type(e).__name__}",
                elapsed_ms(start),
            )
