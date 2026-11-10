"""Provider connection test: the same code path as real routing.

Why this file exists: the Providers page used to show a green "Configured"
badge after a save, which only meant *a database row exists*. Nothing verified
the key, so the first real signal was a 404 in the middle of a prompt — and
that 404 was reported as a bad key.

The rule here is that a test must not be able to disagree with production:

*   same URL builder (``ProviderSpec.chat_url``),
*   same model resolution (``resolve_model`` → discovery + preference order),
*   same provider class (``PROVIDER_REGISTRY[name].generate``),
*   same outcome classification (``outcome`` on the response).

A 1-token prompt is the only difference, so a passing test predicts a working
route. Everything reported here (model, latency, outcome) comes from that one
live call rather than from stored state.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_key
from app.config import settings
from app.core.provider_key_manager import is_supported_provider
from app.db.session import get_session
from app.models.api_key import ApiKeyDB
from app.providers.model_registry import model_catalog, resolve_model, spec_for
from app.providers.registry import PROVIDER_REGISTRY

router = APIRouter(prefix="/provider-test", tags=["Provider test"])

#: Cheapest possible request that still proves authentication AND model access.
PROBE_PROMPT = "ping"
PROBE_MAX_TOKENS = 1

#: How long the UI should wait before giving up on the whole test.
TIMEOUT_HINT_S = 20

#: Key-specific outcomes. Only these may be described as "your key".
KEY_OUTCOMES = frozenset({"auth_failed", "missing_provider_key"})

#: User-facing verdicts, one per outcome.
VERDICT_BY_OUTCOME = {
    "auth_failed": "auth_failed",
    "missing_provider_key": "auth_failed",
    "model_not_found": "model_not_found",
    "endpoint_not_found": "model_not_found",
    "not_found": "model_not_found",
    "rate_limit_exceeded": "rate_limited",
    "bad_request": "bad_request",
    "provider_server_error": "provider_error",
    "provider_unavailable": "provider_error",
    "unexpected_status": "provider_error",
    "network_error": "network_error",
    "timeout": "network_error",
}


class ProviderTestResponse(BaseModel):
    """Result of one live probe. Contains no secret of any kind."""

    provider: str
    display: str
    #: One of: valid, auth_failed, model_not_found, rate_limited,
    #: bad_request, provider_error, network_error, not_configured.
    verdict: str
    #: True ONLY for a successful probe. A stored-but-wrong key is not "valid".
    key_valid: bool
    model: Optional[str] = None
    latency_ms: Optional[float] = None
    #: Explanation for the user (already redacted, ~200 chars of provider text).
    error: Optional[str] = None
    #: Chat models this key can actually use (empty when discovery failed).
    available_models: list[str] = []
    #: Whether the model was an override, discovered, or the static default.
    model_source: Optional[str] = None


class SetModelRequest(BaseModel):
    model: str


def _display(provider: str) -> str:
    from app.api.setup_routes import PROVIDER_META

    meta = PROVIDER_META.get(provider)
    return meta["display"] if meta else provider


async def _probe(provider: str, api_key: str, db_session: AsyncSession, api_key_row: ApiKeyDB):
    """Run one tiny request through the real provider class."""
    if not is_supported_provider(provider):
        raise HTTPException(status_code=404, detail="Unknown provider")

    from app.core.provider_key_manager import get_provider_key, server_key_for

    # Caller's stored key wins, then the environment — exactly as routing does.
    caller_key = await get_provider_key(db_session, api_key_row.id, provider)
    effective_key = caller_key or server_key_for(provider)
    if not effective_key:
        return ProviderTestResponse(
            provider=provider,
            display=_display(provider),
            verdict="not_configured",
            key_valid=False,
            error=(
                f"missing_provider_key: no key is stored for {provider} and none is "
                f"set in the environment."
            ),
        )

    spec = spec_for(provider)
    override = settings.__dict__.get(f"{provider.upper()}_MODEL") or None
    model, available, source = await resolve_model(provider, effective_key, override=override)

    instance = PROVIDER_REGISTRY[provider]
    response = await instance.generate(
        PROBE_PROMPT,
        provider_api_key=effective_key,
        max_tokens=PROBE_MAX_TOKENS,
        **({"task_type": "general"} if provider == "deepseek" else {}),
    )

    if response.success:
        return ProviderTestResponse(
            provider=provider,
            display=_display(provider),
            verdict="valid",
            key_valid=True,
            model=response.model_name or model,
            latency_ms=round(response.latency_ms, 1),
            available_models=available[:50],
            model_source=source,
        )

    outcome = response.outcome or "provider_error"
    verdict = VERDICT_BY_OUTCOME.get(outcome, "provider_error")

    if outcome in ("model_not_found", "endpoint_not_found"):
        # Refresh the list so the UI can offer a choice immediately rather than
        # showing an empty dropdown after telling the user to pick a model.
        model_catalog.invalidate(provider)
        refreshed, _source = await model_catalog.list_models(
            spec, effective_key, force_refresh=True
        ) if spec else ([], "unavailable")
        available = refreshed or available

    return ProviderTestResponse(
        provider=provider,
        display=_display(provider),
        verdict=verdict,
        key_valid=False,
        model=response.model_name or model,
        latency_ms=round(response.latency_ms, 1),
        error=response.error,
        available_models=available[:50],
        model_source=source,
    )


@router.post("/{provider}", response_model=ProviderTestResponse)
async def test_provider(
    provider: str,
    api_key_row: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> ProviderTestResponse:
    """Live probe of one provider using the caller's stored key.

    Returns the real classification, never a guess from "a row exists".
    """
    return await _probe(provider, "", db_session, api_key_row)


@router.get("/{provider}/models", response_model=list[str])
async def list_provider_models(
    provider: str,
    refresh: bool = False,
    api_key_row: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> list[str]:
    """Chat models available to the caller's key — the model dropdown's source."""
    if not is_supported_provider(provider):
        raise HTTPException(status_code=404, detail="Unknown provider")
    from app.core.provider_key_manager import get_provider_key, server_key_for

    key = await get_provider_key(db_session, api_key_row.id, provider) or server_key_for(provider)
    if not key:
        raise HTTPException(status_code=409, detail="No key is configured for this provider.")

    spec = spec_for(provider)
    if spec is None:  # pragma: no cover - guarded above
        raise HTTPException(status_code=404, detail="Unknown provider")
    models, _source = await model_catalog.list_models(spec, key, force_refresh=refresh)
    return models[:50]
