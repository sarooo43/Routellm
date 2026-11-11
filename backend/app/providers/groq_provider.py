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


class GroqProvider(ModelProvider):
    provider_name = "groq"
    DEFAULT_MODEL = "llama-3.3-70b-versatile"
    API_URL = "https://api.groq.com/openai/v1/chat/completions"
    TIMEOUT = 15.0

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        model = kwargs.get("model", self.DEFAULT_MODEL)
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": kwargs.get("temperature", 0.7),
        }
        if kwargs.get("max_tokens") is not None:
            payload["max_tokens"] = kwargs["max_tokens"]
        api_key = kwargs.get("provider_api_key") or settings.GROQ_API_KEY
        headers = {
            "Authorization": f"Bearer {api_key}",
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
                # Honest taxonomy: a 404 (or a body that explicitly says the
                # model does not exist) is a MODEL problem, never an auth
                # problem. The provider message is preserved (sanitized,
                # ~200 chars) for candidate walking.
                detail = ""
                try:
                    data = response.json()
                    detail = str((data.get("error") or {}).get("message") or "")
                except ValueError:
                    detail = ""
                detail = detail.strip()[:200]
                lowered = detail.lower()
                if response.status_code == 404 or (
                    "model" in lowered
                    and ("not exist" in lowered or "does not exist" in lowered
                         or "not found" in lowered)
                ):
                    return make_error(
                        self.provider_name,
                        model,
                        f"model_not_found: {detail}".rstrip(": "),
                        latency_ms,
                    )
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
                f"unexpected_error: {type(e).__name__}",
                elapsed_ms(start),
            )
