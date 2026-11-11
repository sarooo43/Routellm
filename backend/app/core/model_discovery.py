"""Model explorer + candidate walking — a THIN layer over the model registry.

Why this module exists
======================
A provider's list-models response is a *candidate list*, not a guarantee:
Google (and others) roll models out per account cohort, so a brand-new key
can SEE ``models/gemini-2.5-flash`` in its list and still get a 404
("no longer available to new users") when calling it. Rather than picking
one model and failing, everything here treats the discovered list as an
ordered set of candidates:

1. Rank candidates via :func:`app.providers.model_registry.rank_candidate_ids`
   (owner-editable preference tables + numeric version parse: 3.8 > 2.5 >
   2.0, 10.0 > 9.0). ONE ranking implementation — ``choose_model`` delegates
   to the same function, so the Test button and routing cannot disagree.
2. Try up to ``MAX_CANDIDATE_ATTEMPTS`` per request. When a call fails with
   ``model_not_found`` ONLY (never auth/rate-limit), the model is remembered
   as "denied" for this key (~1h TTL) and the next candidate is tried.
3. If the provider's error message names a replacement model
   ("use models/gemini-3.8-flash"), it is extracted with a STRICT regex,
   verified against the discovered list, and tried first. Nothing else from
   an error message is ever trusted or executed.

What each provider's list endpoint really exposes (documented, no invented
fields — each row is "documented" unless marked otherwise):

========== =========================== ==================================
Provider   Fields available            Missing
========== =========================== ==================================
Gemini     name, displayName,          creation timestamp, pricing
           description, inputTokenLimit,
           outputTokenLimit,
           supportedGenerationMethods
           (chat-capable iff
           "generateContent" present)
Groq       id, object, created (unix), pricing
           owned_by, active,
           context_window,
           max_completion_tokens; NO
           capability field (chat
           filtering is name-pattern
           based — whisper/tts/guard
           appear in the list)
DeepSeek   id, object, created (unix), pricing, context window;
           owned_by                    everything listed is chat
========== =========================== ==================================

Tier ("free"/"paid") comes from the static ESTIMATE table in
:mod:`app.config` and is always labeled ``tier_source="estimated"``.

There is exactly ONE discovery engine: the fetch, the in-memory cache and
the DB row live in :mod:`app.providers.model_registry`
(``ModelCatalog`` / ``DiscoveredModelCacheDB``). This module adds ranking,
per-model test statuses (denied/rate_limited), pins, replacement-ID
extraction and the candidate walk on top of it — no second cache, no second
table for the same data.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import settings
from app.models.provider_model_status import (
    ProviderModelPinDB,
    ProviderModelStatusDB,
)
from app.providers.base import ModelProvider, ProviderResponse
from app.providers.model_registry import (
    estimated_tier,
    key_fingerprint,
    model_catalog,
    parse_version_tuple,
    preference_group,
    rank_candidate_ids,
    spec_for,
)
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

__all__ = [
    "DiscoveredModel",
    "DiscoveryError",
    "WalkResult",
    "WalkStep",
    "clear_caches",
    "denied_models",
    "discover_models",
    "extract_replacement_model",
    "generate_with_walking",
    "get_cached_status",
    "get_provider_model_pin",
    "is_valid_model_id",
    "key_fingerprint",
    "load_db_statuses",
    "load_provider_pins",
    "mark_status",
    "operator_env_key",
    "operator_env_model",
    "parse_version_tuple",
    "preference_group",
    "rank_candidates",
    "sanitize_error_message",
    "sort_models_for_provider",
    "upsert_db_status",
    "walk_candidates",
]

logger = get_logger(__name__)

# The ONLY characters a model id may contain (also enforced at the API
# boundary). Error-message text is never trusted beyond a match against
# this pattern PLUS existence in the discovered list.
MODEL_ID_RE = re.compile(r"^[a-zA-Z0-9._:/-]{1,120}$")

MAX_CANDIDATE_ATTEMPTS = 3


class DiscoveryError(Exception):
    """Raised when list-models fails. ``reason`` is sanitized (never a key)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class DiscoveredModel:
    """One model from a provider's list-models response, normalized."""

    id: str
    display_name: str
    # Numeric version tuple parsed from the id, e.g. gemini-3.8-flash ->
    # (3, 8). None when the id carries no version.
    version: Optional[tuple[int, ...]]
    # Unix timestamp when the API provides one (Groq/DeepSeek do; Gemini
    # does not).
    created: Optional[int]
    context_window: Optional[int]
    max_output: Optional[int]
    chat_capable: bool
    capabilities: list[str] = field(default_factory=list)

    @property
    def sort_key(self) -> tuple:
        """Default "newest" ordering key (descending)."""
        return (self.version or (), self.created or 0, self.id)


@dataclass
class WalkStep:
    """One candidate in a walk: attempted+result, or skipped with reason."""

    model: str
    skipped: bool
    reason: str
    success: bool = False
    error: Optional[str] = None
    latency_ms: float = 0.0


@dataclass
class WalkResult:
    response: ProviderResponse
    steps: list[WalkStep]
    model_used: Optional[str]
    replacement_hint_used: Optional[str] = None


# --------------------------------------------------------------------------
# Pure helpers (unit-tested, no I/O)
# --------------------------------------------------------------------------


def is_valid_model_id(model_id: str) -> bool:
    """Strict charset check for a client-supplied model id."""
    if not isinstance(model_id, str) or not MODEL_ID_RE.match(model_id):
        return False
    # Path traversal / weird segments are rejected even though the charset
    # technically allows '.' and '/'.
    if ".." in model_id:
        return False
    # A scheme separator means someone pasted a URL, not a model id.
    if "://" in model_id:
        return False
    return True


def extract_replacement_model(
    message: str,
    discovered_ids: set[str],
    failed_model: Optional[str] = None,
) -> Optional[str]:
    """Extract a replacement model id from a provider error message.

    STRICT: only text shaped like ``models/<id>`` (or ``use <id>``) built
    from the allowed charset, with no ".." segments and no URL context, is
    even considered — and the candidate must exist in the key's discovered
    list to be used. A malicious message ("use models/../../etc/passwd",
    "use https://evil.example/models/x") yields None. The model that just
    failed is never returned as its own replacement.
    """
    if not message:
        return None
    text = message[:500]
    candidates: list[str] = []
    # (?<![\w/.-]) keeps URL hosts ("evil.example/models/x") out: a real
    # provider message always has a delimiter (space, quote, start) before
    # "models/".
    for match in re.finditer(
        r"(?<![\w/.-])models/([a-zA-Z0-9._:/-]{1,120})", text
    ):
        candidate = match.group(1).rstrip("./")
        if candidate:
            candidates.append(candidate)
    for match in re.finditer(
        r"\buse\s+([a-zA-Z0-9._:/-]{1,120})", text, re.IGNORECASE
    ):
        candidate = match.group(1).rstrip("./")
        candidate = candidate.removeprefix("models/")
        if candidate:
            candidates.append(candidate)
    # The replacement is normally named LAST ("use models/gemini-3.8-flash");
    # scan from the end and never return the model that just failed.
    for candidate in reversed(candidates):
        if not is_valid_model_id(candidate):
            continue
        if failed_model and candidate == failed_model:
            continue
        if candidate in discovered_ids:
            return candidate
    return None


# --------------------------------------------------------------------------
# Operator (non-BYOK) settings access
# --------------------------------------------------------------------------


def operator_env_key(provider_name: str) -> Optional[str]:
    """Operator (non-BYOK) credential from settings."""
    env_name = f"{provider_name.upper()}_API_KEY"
    return getattr(settings, env_name, None) or None


def operator_env_model(provider_name: str) -> Optional[str]:
    """Optional .env model override (precedence: pin, then this, then Auto)."""
    return getattr(settings, f"{provider_name.upper()}_MODEL", None) or None


# --------------------------------------------------------------------------
# Per-(key, provider, model) statuses: denied / rate_limited / works (~1h TTL)
# --------------------------------------------------------------------------

_status_cache: dict[tuple[str, str], dict[str, tuple[str, float]]] = {}


def _now() -> float:
    return time.monotonic()


def get_cached_status(api_key: str, provider: str, model_id: str) -> Optional[str]:
    entries = _status_cache.get((key_fingerprint(api_key), provider), {})
    entry = entries.get(model_id)
    if entry is None:
        return None
    status, expires_at = entry
    if expires_at < _now():
        entries.pop(model_id, None)
        return None
    return status


def denied_models(api_key: str, provider: str) -> set[str]:
    entries = _status_cache.get((key_fingerprint(api_key), provider), {})
    now = _now()
    return {
        model_id
        for model_id, (status, expires_at) in entries.items()
        if status == "denied" and expires_at >= now
    }


def mark_status(
    api_key: str, provider: str, model_id: str, status: str
) -> None:
    """Remember a per-(key, provider, model) status in memory for ~1h."""
    if not is_valid_model_id(model_id):
        return
    cache_key = (key_fingerprint(api_key), provider)
    _status_cache.setdefault(cache_key, {})[model_id] = (
        status,
        _now() + settings.MODEL_STATUS_TTL_S,
    )


def clear_caches() -> None:
    """Test hook: wipe the in-memory status cache (discovery lives in the
    model registry's catalog, which has its own clear())."""
    _status_cache.clear()
    model_catalog.clear()


def sanitize_error_message(message: str, api_key: Optional[str] = None) -> str:
    """Truncate a provider message to ~200 chars and scrub the key."""
    text = message or ""
    if api_key and api_key in text:
        text = text.replace(api_key, "***")
    return text.strip()[:200]


# --------------------------------------------------------------------------
# Discovery — a thin adapter over ModelCatalog (the ONE discovery engine)
# --------------------------------------------------------------------------


@dataclass
class _FailureEntry:
    reason: str
    expires_at: float


# Negative cache: a failed list-models call is not retried for 60s so UI
# polling cannot hammer the provider. Not a duplicate of the model cache —
# it stores failure reasons only.
_discovery_failures: dict[tuple[str, str], _FailureEntry] = {}


def _record_failure(cache_key: tuple[str, str], reason: str, ttl: float = 60.0) -> None:
    _discovery_failures[cache_key] = _FailureEntry(reason, _now() + ttl)


def _record_to_model(provider: str, record: dict) -> Optional[DiscoveredModel]:
    """Normalize one discovery record. None = drop (non-chat / malformed)."""
    model_id = str(record.get("id") or "").strip()
    if not model_id or not is_valid_model_id(model_id):
        return None
    created = record.get("created")
    if not isinstance(created, int) or isinstance(created, bool):
        created = None
    context = record.get("context_window")
    max_out = record.get("max_output")
    capabilities = record.get("capabilities") or []
    if not isinstance(capabilities, list):
        capabilities = []
    display = str(record.get("display_name") or model_id)
    # Chat capability was decided by the registry's non-chat-marker filter at
    # discovery time; every record handed here is chat-capable.
    return DiscoveredModel(
        id=model_id,
        display_name=display[:120],
        version=parse_version_tuple(model_id),
        created=created,
        context_window=context if isinstance(context, int) else None,
        max_output=max_out if isinstance(max_out, int) else None,
        chat_capable=True,
        capabilities=[str(m)[:40] for m in capabilities if m],
    )


async def discover_models(
    provider: str, api_key: str, force: bool = False
) -> tuple[list[DiscoveredModel], Optional[str]]:
    """Fetch + normalize the provider's list-models response.

    Returns (models, None) on success or ([], reason) on failure (the
    failure is negatively cached for 60s so UI polling cannot hammer the
    provider). Never raises; never includes the key in the reason.

    Delegates the actual fetch + caching to ``model_catalog.list_records``
    so there is exactly one list-models call, one cache and one DB row.
    """
    spec = spec_for(provider)
    if spec is None:
        return [], f"unknown provider {provider!r}"

    cache_key = (key_fingerprint(api_key), provider)
    if not force:
        failed = _discovery_failures.get(cache_key)
        if failed and failed.expires_at > _now():
            return [], failed.reason

    try:
        records, _source = await model_catalog.list_records(spec, api_key, force_refresh=force)
    except Exception as e:  # noqa: BLE001 - discovery is fail-soft
        reason = f"discovery_failed: {type(e).__name__}"
        _record_failure(cache_key, reason)
        return [], reason

    if not records:
        # ModelCatalog could not fetch (network/auth/429). Reason is generic;
        # the registry never surfaces the body.
        reason = (
            "discovery_failed: the provider's list-models endpoint did not "
            "return usable models (bad key, throttling, or network trouble)"
        )
        _record_failure(cache_key, reason)
        return [], reason

    models: list[DiscoveredModel] = []
    for record in records:
        parsed = _record_to_model(provider, record) if isinstance(record, dict) else None
        if parsed is not None:
            models.append(parsed)

    _discovery_failures.pop(cache_key, None)
    return models, None


def rank_candidates(
    provider: str, models: list[DiscoveredModel], denied: set[str]
) -> list[DiscoveredModel]:
    """Order candidates for the walk: the shared registry ranking, minus
    anything this key was already denied."""
    spec = spec_for(provider)
    if spec is None:
        return []
    created = {m.id: (m.created or 0) for m in models}
    ordered_ids = rank_candidate_ids(spec, [m.id for m in models], created=created)
    by_id = {m.id: m for m in models}
    return [by_id[m] for m in ordered_ids if m not in denied and m in by_id]


def sort_models_for_provider(
    models: list[DiscoveredModel],
    provider: str,
    sort: str = "tier",
    tier_filter: str = "all",
    search: str = "",
) -> tuple[list[DiscoveredModel], int]:
    """Sort + filter for the explorer endpoint. Returns (models, hidden).

    ``sort``: tier (free first, then newest), newest, oldest, name.
    ``tier_filter``: all | free | paid. ``search``: case-insensitive
    substring on id/display_name.
    """
    visible = [m for m in models if m.chat_capable]
    hidden = len(models) - len(visible)

    if search:
        needle = search.lower().strip()
        visible = [
            m for m in visible
            if needle in m.id.lower() or needle in m.display_name.lower()
        ]

    if tier_filter in ("free", "paid"):
        visible = [
            m for m in visible if estimated_tier(provider, m.id) == tier_filter
        ]

    def tier_then_newest(model: DiscoveredModel):
        tier_value = estimated_tier(provider, model.id)
        rank = {"free": 0, "paid": 1, "unknown": 2}.get(tier_value, 2)
        return (rank, tuple(-part for part in (model.version or ())), -(model.created or 0), model.id)

    if sort == "newest":
        visible.sort(key=lambda m: m.sort_key, reverse=True)
    elif sort == "oldest":
        visible.sort(key=lambda m: m.sort_key)
    elif sort == "name":
        visible.sort(key=lambda m: m.id.lower())
    else:  # default "tier"
        visible.sort(key=tier_then_newest)

    return visible, hidden


# --------------------------------------------------------------------------
# DB-backed status / pin helpers
# --------------------------------------------------------------------------


async def load_db_statuses(
    db_session: AsyncSession, api_key_id: int, provider: str
) -> dict[str, ProviderModelStatusDB]:
    stmt = select(ProviderModelStatusDB).where(
        ProviderModelStatusDB.api_key_id == api_key_id,
        ProviderModelStatusDB.provider_name == provider,
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    return {row.model_id: row for row in rows}


async def upsert_db_status(
    db_session: AsyncSession,
    api_key_id: int,
    provider: str,
    model_id: str,
    status: str,
    error: Optional[str],
) -> None:
    """Persist one test/status observation. Best-effort: never raises."""
    try:
        stmt = select(ProviderModelStatusDB).where(
            ProviderModelStatusDB.api_key_id == api_key_id,
            ProviderModelStatusDB.provider_name == provider,
            ProviderModelStatusDB.model_id == model_id,
        )
        row = (await db_session.execute(stmt)).scalars().first()
        if row is None:
            row = ProviderModelStatusDB(
                api_key_id=api_key_id,
                provider_name=provider,
                model_id=model_id,
            )
        from app.models.api_key import utcnow

        row.status = status
        row.last_checked_at = utcnow()
        row.last_error = sanitize_error_message(error or "") if error else None
        db_session.add(row)
        await db_session.commit()
    except Exception as e:
        await db_session.rollback()
        logger.warning("model_status_persist_failed", provider=provider, error=str(e))


async def get_provider_model_pin(
    db_session: AsyncSession, api_key_id: int, provider: str
) -> Optional[str]:
    if db_session is None:
        return None
    try:
        stmt = select(ProviderModelPinDB).where(
            ProviderModelPinDB.api_key_id == api_key_id,
            ProviderModelPinDB.provider_name == provider,
        )
        row = (await db_session.execute(stmt)).scalars().first()
        return row.model_id if row else None
    except Exception as e:
        logger.warning("model_pin_read_failed", provider=provider, error=str(e))
        return None


async def load_provider_pins(
    db_session: AsyncSession, api_key_id: Optional[int]
) -> dict[str, str]:
    """All pinned models for one caller key: {provider: model_id}.

    Routing precedence: pinned model, then the .env override, then Auto.
    """
    if api_key_id is None:
        return {}
    try:
        stmt = select(ProviderModelPinDB).where(
            ProviderModelPinDB.api_key_id == api_key_id
        )
        rows = (await db_session.execute(stmt)).scalars().all()
        return {row.provider_name: row.model_id for row in rows}
    except Exception as e:
        logger.warning("model_pins_read_failed", error=str(e))
        return {}


# --------------------------------------------------------------------------
# Error classification bridge
# --------------------------------------------------------------------------


def _is_model_not_found(response: ProviderResponse) -> bool:
    """The machine-readable outcome decides; prose is only a fallback.

    PR #15's providers classify HTTP responses into ``response.outcome``;
    the walk treats ONLY model-targeting failures as walkable. DeepSeek's
    400 "Model Not Exist" is also a targeting failure and outcomes.py maps
    it there (see ``app.providers.outcomes``).
    """
    if response.outcome == "model_not_found":
        return True
    return bool((response.error or "").startswith("model_not_found"))


def _is_rate_limited(response: ProviderResponse) -> bool:
    if response.outcome == "rate_limit_exceeded":
        return True
    return bool((response.error or "") == "rate_limit_exceeded")


# --------------------------------------------------------------------------
# The shared candidate walk (routing + Test button use THIS code path)
# --------------------------------------------------------------------------


async def walk_candidates(
    provider_name: str,
    prompt: str,
    kwargs: dict,
    api_key: str,
    first_model: Optional[str] = None,
    max_attempts: int = MAX_CANDIDATE_ATTEMPTS,
    db_session: Optional[AsyncSession] = None,
    api_key_id: Optional[int] = None,
    provider: Optional[ModelProvider] = None,
) -> WalkResult:
    """Execute a prompt against one provider, walking ordered candidates.

    Candidate order: explicit ``first_model`` (a caller's pin or an .env
    override), then rank_candidates() over the key's discovered models.
    ``model_not_found`` marks the model denied for this key (~1h TTL) and
    moves to the next candidate; a replacement id named in the provider's
    error message (strict-regex extracted + verified against the discovered
    list) is tried first. Any OTHER error (rate limit, auth, timeout,
    server error) ends the walk so the executor can fall through to the
    next provider as before.
    """
    provider_obj = provider or PROVIDER_REGISTRY.get(provider_name)
    if provider_obj is None:
        raise ValueError(f"unknown provider {provider_name!r}")

    models, discovery_reason = await discover_models(provider_name, api_key)
    if discovery_reason and not models:
        logger.warning(
            "model_discovery_failed_falling_back_to_default",
            provider=provider_name,
            reason=discovery_reason,
        )
    discovered_ids = {m.id for m in models}
    denied = denied_models(api_key, provider_name)

    ordered: list[str] = []
    if first_model and is_valid_model_id(first_model):
        ordered.append(first_model)
    ordered.extend(
        m.id for m in rank_candidates(provider_name, models, denied)
        if m.id not in ordered
    )
    # Nothing discovered at all? Still try the caller's/kwargs model or the
    # provider default once, so a totally failed discovery degrades to
    # legacy behavior instead of skipping the provider.
    if not ordered:
        fallback = (
            kwargs.get("model")
            or getattr(provider_obj, "DEFAULT_MODEL", None)
            or ""
        )
        ordered = [fallback]

    steps: list[WalkStep] = []
    replacement_hint: Optional[str] = None
    tried: set[str] = set()

    while ordered and len(tried) < max_attempts:
        model_id = ordered.pop(0)
        if not model_id or model_id in tried:
            continue
        tried.add(model_id)

        if model_id in denied:
            steps.append(
                WalkStep(
                    model=model_id,
                    skipped=True,
                    reason="denied: a previous call returned model_not_found for this key",
                )
            )
            continue

        call_kwargs = dict(kwargs)
        call_kwargs["model"] = model_id
        response = await provider_obj.generate(prompt, **call_kwargs)

        error = response.error or ""
        if response.success:
            mark_status(api_key, provider_name, model_id, "works")
            if db_session is not None and api_key_id is not None:
                await upsert_db_status(
                    db_session, api_key_id, provider_name, model_id, "works", None
                )
            steps.append(
                WalkStep(
                    model=model_id,
                    skipped=False,
                    reason="",
                    success=True,
                    latency_ms=response.latency_ms,
                )
            )
            return WalkResult(
                response=response,
                steps=steps,
                model_used=model_id,
                replacement_hint_used=replacement_hint,
            )

        if _is_model_not_found(response):
            # Honest classification: only model_not_found marks denied.
            mark_status(api_key, provider_name, model_id, "denied")
            if db_session is not None and api_key_id is not None:
                await upsert_db_status(
                    db_session,
                    api_key_id,
                    provider_name,
                    model_id,
                    "denied",
                    error,
                )
            steps.append(
                WalkStep(
                    model=model_id,
                    skipped=False,
                    reason="model_not_found",
                    success=False,
                    error=error,
                    latency_ms=response.latency_ms,
                )
            )
            hint = extract_replacement_model(
                error.split(":", 1)[1] if ":" in error else error,
                discovered_ids,
                failed_model=model_id,
            )
            if hint and hint not in tried:
                replacement_hint = hint
                ordered.insert(0, hint)
            continue

        # Any other failure: mark rate_limited when applicable, stop the
        # walk (the executor's provider fallback takes over).
        if _is_rate_limited(response):
            mark_status(api_key, provider_name, model_id, "rate_limited")
            if db_session is not None and api_key_id is not None:
                await upsert_db_status(
                    db_session,
                    api_key_id,
                    provider_name,
                    model_id,
                    "rate_limited",
                    error,
                )
        steps.append(
            WalkStep(
                model=model_id,
                skipped=False,
                reason=error or "unknown_error",
                success=False,
                error=error,
                latency_ms=response.latency_ms,
            )
        )
        break

    last = steps[-1] if steps else None
    final_response = ProviderResponse(
        success=False,
        text="",
        tokens_used=0,
        latency_ms=last.latency_ms if last else 0.0,
        error=(last.error if last and last.error else "all_candidates_failed"),
        provider_name=provider_name,
        model_name=(last.model if last else "unknown"),
    )
    return WalkResult(
        response=final_response,
        steps=steps,
        model_used=None,
        replacement_hint_used=replacement_hint,
    )


async def generate_with_walking(
    provider_name: str,
    prompt: str,
    kwargs: dict,
    api_key: str,
    pinned_model: Optional[str] = None,
    max_attempts: int = MAX_CANDIDATE_ATTEMPTS,
    provider: Optional[ModelProvider] = None,
) -> tuple[ProviderResponse, list[WalkStep], Optional[str]]:
    """Routing-time execution: lazy candidate walking.

    The FIRST attempt keeps legacy behavior (zero discovery overhead for
    healthy requests): pinned model, else the .env override, else the
    top of an already-WARM discovery cache, else the provider default.

    Only when that call fails with model_not_found does discovery run;
    the remaining ranked candidates are then tried (up to max_attempts
    total). A replacement id named in the provider's error message is
    tried first. Returns (response, extra_steps, initial_model) where
    extra_steps EXCLUDES the initial attempt (the executor logs that one
    itself) and initial_model is the model id used for attempt #1 (None =
    provider default).
    """
    provider_obj = provider or PROVIDER_REGISTRY.get(provider_name)
    if provider_obj is None:
        raise ValueError(f"unknown provider {provider_name!r}")

    initial_model: Optional[str] = pinned_model
    if initial_model is None:
        initial_model = operator_env_model(provider_name)

    # If the model we were going to try first is already denied for this
    # key (~1h TTL), don't waste the call: go straight to the ranked walk.
    if initial_model and initial_model in denied_models(api_key, provider_name):
        walk = await walk_candidates(
            provider_name,
            prompt,
            kwargs,
            api_key,
            first_model=None,
            max_attempts=max_attempts,
            provider=provider_obj,
        )
        return walk.response, walk.steps, walk.model_used

    if initial_model is None:
        # Warm-cache top only: no live discovery call on the hot path.
        spec = spec_for(provider_name)
        if spec is not None:
            cached_ids = model_catalog.peek(provider_name, api_key) or []
            ranked = rank_candidate_ids(
                spec,
                cached_ids,
                created={
                    m.get("id"): (m.get("created") or 0)
                    for m in model_catalog.peek_meta(provider_name, api_key).values()
                    if isinstance(m, dict)
                },
            )
            denied_now = denied_models(api_key, provider_name)
            for model_id in ranked:
                if model_id and is_valid_model_id(model_id) and model_id not in denied_now:
                    initial_model = model_id
                    break

    call_kwargs = dict(kwargs)
    if initial_model:
        call_kwargs["model"] = initial_model
    else:
        initial_model = getattr(provider_obj, "DEFAULT_MODEL", None)

    # Same guard for the provider-default fallback above.
    if initial_model and initial_model in denied_models(api_key, provider_name):
        walk = await walk_candidates(
            provider_name,
            prompt,
            kwargs,
            api_key,
            first_model=None,
            max_attempts=max_attempts,
            provider=provider_obj,
        )
        return walk.response, walk.steps, walk.model_used

    response = await provider_obj.generate(prompt, **call_kwargs)
    if response.success or not _is_model_not_found(response):
        if response.success and initial_model:
            mark_status(api_key, provider_name, initial_model, "works")
        elif _is_rate_limited(response) and initial_model:
            mark_status(api_key, provider_name, initial_model, "rate_limited")
        return response, [], initial_model

    # ---- model_not_found: discover and walk the remaining candidates ----
    denied = denied_models(api_key, provider_name)
    if initial_model:
        denied = set(denied) | {initial_model}
        mark_status(api_key, provider_name, initial_model, "denied")

    models, discovery_reason = await discover_models(provider_name, api_key)
    if discovery_reason and not models:
        logger.warning(
            "model_discovery_failed_cannot_walk",
            provider=provider_name,
            reason=discovery_reason,
        )
        return response, [], initial_model

    discovered_ids = {m.id for m in models}
    ordered = [
        m.id for m in rank_candidates(provider_name, models, denied)
    ]
    steps: list[WalkStep] = [
        WalkStep(
            model=initial_model or "provider-default",
            skipped=False,
            reason="model_not_found",
            success=False,
            error=response.error,
            latency_ms=response.latency_ms,
        )
    ]
    replacement_hint: Optional[str] = None
    tried: set[str] = {initial_model or ""}
    attempts_after_first = 1  # the initial failed attempt counts

    while ordered and attempts_after_first < max_attempts:
        model_id = ordered.pop(0)
        if not model_id or model_id in tried:
            continue
        tried.add(model_id)

        if model_id in denied:
            steps.append(
                WalkStep(
                    model=model_id,
                    skipped=True,
                    reason="denied: a previous call returned model_not_found for this key",
                )
            )
            continue

        attempts_after_first += 1
        call_kwargs = dict(kwargs)
        call_kwargs["model"] = model_id
        response = await provider_obj.generate(prompt, **call_kwargs)

        if response.success:
            mark_status(api_key, provider_name, model_id, "works")
            steps.append(
                WalkStep(
                    model=model_id,
                    skipped=False,
                    reason="",
                    success=True,
                    latency_ms=response.latency_ms,
                )
            )
            return response, steps, initial_model

        if _is_model_not_found(response):
            mark_status(api_key, provider_name, model_id, "denied")
            steps.append(
                WalkStep(
                    model=model_id,
                    skipped=False,
                    reason="model_not_found",
                    success=False,
                    error=response.error,
                    latency_ms=response.latency_ms,
                )
            )
            hint = extract_replacement_model(
                (response.error or "").split(":", 1)[1]
                if ":" in (response.error or "")
                else (response.error or ""),
                discovered_ids,
                failed_model=model_id,
            )
            if hint and hint not in tried:
                replacement_hint = hint
                ordered.insert(0, hint)
            continue

        if _is_rate_limited(response):
            mark_status(api_key, provider_name, model_id, "rate_limited")
        steps.append(
            WalkStep(
                model=model_id,
                skipped=False,
                reason=response.error or "unknown_error",
                success=False,
                error=response.error,
                latency_ms=response.latency_ms,
            )
        )
        break  # non-model errors: let the executor fall through to the next provider

    return response, steps, initial_model
