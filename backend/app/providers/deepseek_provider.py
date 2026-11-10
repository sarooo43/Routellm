import time

import httpx

from app.config import settings
from app.providers.base import ModelProvider, ProviderResponse, elapsed_ms, make_success
from app.providers.model_registry import PROVIDER_SPECS, resolve_model, spec_for
from app.providers.outcomes import error_for_exception, error_for_status

SPEC = PROVIDER_SPECS["deepseek"]

#: Task-specific aliases. ``deepseek-coder`` is retained as a preference only
#: when the account still offers it; discovery filters it out when it does not,
#: which is exactly the failure this replaced (a retired alias returning 404
#: for every code prompt).
CODE_MODEL_PREFERENCE = "deepseek-coder"


class DeepSeekProvider(ModelProvider):
    provider_name = "deepseek"
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

    def _task_override(self, kwargs: dict) -> str | None:
        """Explicit model > model_variant > the code-task preference.

        ``model_variant`` is the legacy name the executor used; it is honoured
        but no longer the only way to steer the model.
        """
        explicit = kwargs.get("model") or kwargs.get("model_variant")
        if explicit:
            return explicit
        return None

    async def generate(self, prompt: str, **kwargs) -> ProviderResponse:
        api_key = kwargs.get("provider_api_key") or settings.DEEPSEEK_API_KEY
        model, available, _source = await self.resolve_model(
            api_key,
            override=self._task_override(kwargs) or settings.DEEPSEEK_MODEL or None,
            force_refresh=bool(kwargs.get("refresh_model")),
        )

        # A code-task PREFERENCE, applied only when the account actually lists
        # that model. The old code let the executor pass "deepseek-coder" as an
        # explicit model, which skipped discovery and 404'd for every code
        # prompt once the alias was retired.
        wants_code_model = bool(kwargs.get("prefer_code_model")) or (
            kwargs.get("task_type") == "code"
        )
        if (
            wants_code_model
            and not kwargs.get("model")
            and not kwargs.get("model_variant")
            and not settings.DEEPSEEK_MODEL
            and CODE_MODEL_PREFERENCE in available
        ):
            model = CODE_MODEL_PREFERENCE

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
