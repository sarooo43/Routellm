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


class DeepSeekProvider(ModelProvider):
    provider_name = "deepseek"
    DEFAULT_MODEL = "deepseek-chat"
    API_URL = "https://api.deepseek.com/v1/chat/completions"
    TIMEOUT = 15.0

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        model = kwargs.get("model_variant", self.DEFAULT_MODEL)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get("temperature", 0.7),
        }
        headers = {
            "Authorization": f"Bearer {settings.DEEPSEEK_API_KEY}",
            "Content-Type": "application/json",
        }

        start = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                response = await client.post(
                    self.API_URL, json=payload, headers=headers
                )
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
            text = data["choices"][0]["message"]["content"]
            usage = data.get("usage") or {}
            tokens_used = usage.get("total_tokens", 0)
            return make_success(
                self.provider_name, model, text, tokens_used, latency_ms
            )
        except httpx.TimeoutException:
            return make_error(self.provider_name, model, "timeout", elapsed_ms(start))
        except Exception as e:
            return make_error(
                self.provider_name,
                model,
                f"unexpected_error: {e}",
                elapsed_ms(start),
            )
