"""Provider connection test: the same code path as real routing.

Why this file exists: the Providers page used to show a green "Configured"
badge after a save, which only meant *a database row exists*. Nothing verified
the key, so the first real signal was a 404 in the middle of a prompt — and
that 404 was reported as a bad key.

The rule here is that a test must not be able to disagree with production:

*   same URL builder (``ProviderSpec.chat_url``),
*   same candidate ranking (``model_registry.rank_candidate_ids`` — one
    ordering for the explorer, the walk and ``choose_model``),
*   same provider class (``PROVIDER_REGISTRY[name].generate``),
*   same outcome classification (``outcome`` on the response),
*   in Auto mode the SAME candidate walk routing uses, so "denied" models are
    skipped exactly as a real route would skip them.

A 1-token prompt is the only difference, so a passing test predicts a working
route. Everything reported here (model, latency, outcome, the walk's
attempted/skipped lists) comes from those live calls rather than stored state.

Because every test spends the user's provider quota, this endpoint is guarded
by :mod:`app.core.test_endpoint_guard`: at most 10 tests per key per minute
and 5 in flight, answered with 429 + Retry-After beyond that.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import require_api_key
from app.core.provider_key_manager import is_supported_provider
from app.core.test_endpoint_guard import check_and_acquire, release
from app.db.session import get_session
from app.models.api_key import ApiKeyDB
from app.providers.model_registry import model_catalog, spec_for
from app.providers.registry import PROVIDER_REGISTRY

router = APIRouter(prefix="/provider-test", tags=["Provider test"])

#: Cheapest possible request that still proves authentication AND model access.
PROBE_PROMPT = "ping"
PROBE_MAX_TOKENS = 1

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


class ProviderTestStep(BaseModel):
    """One candidate in the walk: attempted+result, or skipped with reason."""

    model: str
    skipped: bool
    reason: str
    success: bool = False


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
    #: The candidate walk (Auto mode): which models were tried, which skipped.
    attempted: list[ProviderTestStep] = []
    skipped: list[ProviderTestStep] = []


def _display(provider: str) -> str:
    from app.api.setup_routes import PROVIDER_META

    meta = PROVIDER_META.get(provider)
    return meta["display"] if meta else provider


def _steps(walk) -> tuple[list[ProviderTestStep], list[ProviderTestStep]]:
    attempted = [
        ProviderTestStep(
            model=s.model,
            skipped=False,
            reason=s.reason or ("worked" if s.success else "failed"),
            success=s.success,
        )
        for s in walk.steps
        if not s.skipped
    ]
    skipped = [
        ProviderTestStep(model=s.model, skipped=True, reason=s.reason, success=False)
        for s in walk.steps
        if s.skipped
    ]
    return attempted, skipped


async def _probe(provider: str, api_key: str, db_session: AsyncSession, api_key_row: ApiKeyDB):
    """Run the real path: discovery → ranked walk → outcome classification."""
    if not is_supported_provider(provider):
        raise HTTPException(status_code=404, detail="Unknown provider")

    from app.core.model_discovery import operator_env_model, walk_candidates
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
    override = operator_env_model(provider)

    # ONE code path with routing: the same lazy candidate walk the executor
    # uses, driven by the same ranking. An explicit pin/override is tried
    # first; otherwise the top-ranked discovered model goes first and a
    # model_not_found moves on to the next candidate.
    kwargs = {"provider_api_key": effective_key, "max_tokens": PROBE_MAX_TOKENS}
    if provider == "deepseek":
        kwargs["task_type"] = "general"
    walk = await walk_candidates(
        provider,
        PROBE_PROMPT,
        kwargs,
        effective_key,
        first_model=override,
        provider=PROVIDER_REGISTRY[provider],
    )

    response = walk.response
    if response.success:
        # Discovery already ran (the walk uses it); this is a cache read, not
        # a second provider call.
        available, model_source = ([], None)
        if spec is not None:
            available, model_source = await model_catalog.list_models(spec, effective_key)
        attempted, skipped = _steps(walk)
        return ProviderTestResponse(
            provider=provider,
            display=_display(provider),
            verdict="valid",
            key_valid=True,
            model=walk.model_used or response.model_name,
            latency_ms=round(response.latency_ms, 1),
            available_models=available[:50],
            model_source=model_source,
            attempted=attempted,
            skipped=skipped,
        )

    outcome = response.outcome or "provider_error"
    verdict = VERDICT_BY_OUTCOME.get(outcome, "provider_error")

    available, model_source = ([], None)
    if spec is not None:
        try:
            available, model_source = await model_catalog.list_models(spec, effective_key)
        except Exception:  # noqa: BLE001 - listing is an affordance, not the verdict
            available, model_source = [], None

    attempted, skipped = _steps(walk)
    return ProviderTestResponse(
        provider=provider,
        display=_display(provider),
        verdict=verdict,
        key_valid=False,
        model=response.model_name or walk.model_used,
        latency_ms=round(response.latency_ms, 1),
        error=response.error,
        available_models=available[:50],
        model_source=model_source,
        attempted=attempted,
        skipped=skipped,
    )


@router.post("/{provider}", response_model=ProviderTestResponse)
async def test_provider(
    provider: str,
    api_key_row: ApiKeyDB = Depends(require_api_key),
    db_session: AsyncSession = Depends(get_session),
) -> ProviderTestResponse:
    """Live probe of one provider using the caller's stored key.

    Returns the real classification, never a guess from "a row exists".
    Rate limited to protect the caller's provider quota.
    """
    check_and_acquire(api_key_row.id)
    try:
        return await _probe(provider, "", db_session, api_key_row)
    finally:
        release(api_key_row.id)
