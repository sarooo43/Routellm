"""Model discovery + candidate walking (the "model explorer" engine).

Why this module exists
======================
A provider's list-models response is a *candidate list*, not a guarantee:
Google (and others) roll models out per account cohort, so a brand-new key
can SEE ``models/gemini-2.5-flash`` in its list and still get a 404
("no longer available to new users") when calling it. Rather than picking
one model and failing, everything here treats the discovered list as an
ordered set of candidates:

1. Rank candidates by the owner-editable preference tables in
   :mod:`app.config` (numeric version parse: 3.8 > 2.5 > 2.0, 10.0 > 9.0).
2. Try up to ``MAX_CANDIDATE_ATTEMPTS`` per request. When a call fails with
   ``model_not_found`` ONLY (never auth/rate-limit), the model is remembered
   as "denied" for this key (~1h TTL) and the next candidate is tried.
3. If the provider's error message names a replacement model
   ("use models/gemini-3.8-flash"), it is extracted with a STRICT regex,
   verified against the discovered list, and tried first. Nothing else from
   an error message is ever trusted or executed.

What each provider's list endpoint really exposes (documented, no invented
fields):

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
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.config import (
    MODEL_EXCLUDE_PATTERNS,
    MODEL_PREFERENCE_GROUPS,
    MODEL_TIER_TABLE,
    settings,
)
from app.models.provider_model_status import (
    ProviderModelPinDB,
    ProviderModelStatusDB,
)
from app.providers.base import ModelProvider, ProviderResponse
from app.providers.registry import PROVIDER_REGISTRY
from app.utils.logger import get_logger

logger = get_logger(__name__)

# The ONLY characters a model id may contain (also enforced at the API
# boundary). Error-message text is never trusted beyond a match against
# this pattern PLUS existence in the discovered list.
MODEL_ID_RE = re.compile(r"^[a-zA-Z0-9._:/-]{1,120}$")

_MAX_CANDIDATE_ATTEMPTS = 3

# How a selected model is passed to each provider's generate() kwargs.
MODEL_KWARG = {"gemini": "model", "groq": "model", "deepseek": "model_variant"}

# Env-var fallback keys for operator (non-BYOK) routing.
PROVIDER_ENV_KEY = {
    "groq": "GROQ_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}


def operator_env_key(provider_name: str) -> Optional[str]:
    """Operator (non-BYOK) credential from settings."""
    env_name = PROVIDER_ENV_KEY.get(provider_name)
    return getattr(settings, env_name, None) if env_name else None


def operator_env_model(provider_name: str) -> Optional[str]:
    """Optional .env model override (precedence: pin, then this, then Auto)."""
    return getattr(settings, f"{provider_name.upper()}_MODEL", None) or None

_DISCOVERY_SPECS = {
    "gemini": {
        "url": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        "auth": "header",
        "header": "x-goog-api-key",
    },
    "groq": {
        "url": "https://api.groq.com/openai/v1/models",
        "auth": "bearer",
    },
    "deepseek": {
        "url": "https://api.deepseek.com/models",
        "auth": "bearer",
    },
}


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


def parse_version_tuple(model_id: str) -> Optional[tuple[int, ...]]:
    """Parse the FIRST numeric version group in an id as numbers.

    gemini-3.8-flash -> (3, 8); gemini-2.0-flash -> (2, 0);
    gemini-10.0-flash -> (10, 0) so 10.0 > 9.0 numerically.
    Returns None when the id carries no version.
    """
    match = re.search(r"(?<![\w.])(\d+(?:\.\d+)*)", model_id.lower())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def name_matches_exclude_patterns(model_id: str) -> bool:
    lowered = model_id.lower()
    return any(pattern in lowered for pattern in MODEL_EXCLUDE_PATTERNS)


def preference_group(provider: str, model_id: str) -> tuple[int, str]:
    """(group_index, note) of the first matching preference group."""
    lowered = model_id.lower()
    groups = MODEL_PREFERENCE_GROUPS.get(provider, [{"match": "", "exclude": []}])
    for index, group in enumerate(groups):
        match = group.get("match", "")
        if match and match not in lowered:
            continue
        if any(excl in lowered for excl in group.get("exclude", [])):
            continue
        return index, group.get("note", "")
    return len(groups), "no preference group matched"


def estimated_tier(provider: str, model_id: str) -> str:
    """Static tier ESTIMATE from config. "unknown" when nothing matches."""
    lowered = model_id.lower()
    for entry in MODEL_TIER_TABLE.get(provider, []):
        match = entry.get("match", "")
        if not match or match in lowered:
            return entry.get("tier", "unknown")
    return "unknown"


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


def classify_provider_error(status_code: int, body_text: str) -> Optional[str]:
    """Honest error taxonomy from an upstream generate() response.

    Returns "model_not_found" ONLY for 404 (or 4xx bodies that explicitly
    talk about the model, e.g. DeepSeek's 400 "Model Not Exist"). Auth and
    rate-limit errors are NEVER reclassified as model_not_found.
    """
    lowered = (body_text or "")[:400].lower()
    if status_code == 404:
        return "model_not_found"
    if status_code in (400, 404) and "model" in lowered and (
        "not exist" in lowered
        or "not found" in lowered
        or "no longer available" in lowered
        or "does not exist" in lowered
        or "model_not_found" in lowered
    ):
        return "model_not_found"
    return None


# --------------------------------------------------------------------------
# In-memory caches (per provider-key fingerprint; ~1h TTL)
# --------------------------------------------------------------------------


def key_fingerprint(api_key: str) -> str:
    """Stable, non-reversible cache key for one upstream provider key.

    Only the first 16 hex chars of a SHA-256 are used — enough to scope the
    cache per key, useless for reconstructing the secret.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


_discovery_cache: dict[tuple[str, str], tuple[float, list[DiscoveredModel]]] = {}
_discovery_failure_cache: dict[tuple[str, str], tuple[float, str]] = {}
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
    """Test hook: wipe all in-memory discovery/status caches."""
    _discovery_cache.clear()
    _discovery_failure_cache.clear()
    _status_cache.clear()


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _gemini_chat_capable(raw: dict) -> bool:
    methods = raw.get("supportedGenerationMethods") or []
    return "generateContent" in methods


def _parse_model(provider: str, raw: dict) -> Optional[DiscoveredModel]:
    """Normalize one list-models entry. None = drop (non-chat / malformed)."""
    if provider == "gemini":
        name = str(raw.get("name") or "")
        model_id = name.removeprefix("models/").strip()
        if not model_id:
            return None
        chat = _gemini_chat_capable(raw)
        context = raw.get("inputTokenLimit")
        max_out = raw.get("outputTokenLimit")
        capabilities = [str(m) for m in (raw.get("supportedGenerationMethods") or [])]
        display = str(raw.get("displayName") or model_id)
        created = None
    else:
        model_id = str(raw.get("id") or "").strip()
        if not model_id:
            return None
        # Groq/DeepSeek expose no capability field: chat filtering is
        # name-pattern based (whisper/tts/guard etc. appear in Groq's list).
        chat = not name_matches_exclude_patterns(model_id)
        context = raw.get("context_window")
        max_out = raw.get("max_completion_tokens")
        created = raw.get("created")
        try:
            created = int(created) if created is not None else None
        except (TypeError, ValueError):
            created = None
        capabilities = ["chat"] if chat else []
        display = model_id

    if not chat:
        return None
    if not is_valid_model_id(model_id):
        return None
    return DiscoveredModel(
        id=model_id,
        display_name=display,
        version=parse_version_tuple(model_id),
        created=created,
        context_window=int(context) if isinstance(context, int) else None,
        max_output=int(max_out) if isinstance(max_out, int) else None,
        chat_capable=True,
        capabilities=capabilities,
    )


def sanitize_error_message(message: str, api_key: Optional[str] = None) -> str:
    """Truncate a provider message to ~200 chars and scrub the key."""
    text = message or ""
    if api_key and api_key in text:
        text = text.replace(api_key, "***")
    return text.strip()[:200]


async def discover_models(
    provider: str, api_key: str, force: bool = False
) -> tuple[list[DiscoveredModel], Optional[str]]:
    """Fetch + normalize the provider's list-models response.

    Returns (models, None) on success or ([], reason) on failure (the
    failure is negatively cached for 60s so UI polling cannot hammer the
    provider). Never raises; never includes the key in the reason.
    """
    if provider not in _DISCOVERY_SPECS:
        return [], f"unknown provider {provider!r}"

    cache_key = (key_fingerprint(api_key), provider)
    if not force:
        cached = _discovery_cache.get(cache_key)
        if cached and cached[0] > _now():
            return cached[1], None
        failed = _discovery_failure_cache.get(cache_key)
        if failed and failed[0] > _now():
            return [], failed[1]

    spec = _DISCOVERY_SPECS[provider]
    headers = {"Content-Type": "application/json"}
    if spec["auth"] == "bearer":
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers[spec["header"]] = api_key

    try:
        async with httpx.AsyncClient(timeout=settings.MODEL_DISCOVERY_TIMEOUT_S) as client:
            response = await client.get(spec["url"], headers=headers)
    except httpx.TimeoutException:
        reason = "discovery_timeout: provider list-models did not answer in time"
        _discovery_failure_cache[cache_key] = (_now() + 60.0, reason)
        return [], reason
    except Exception as e:  # network failure — sanitized
        reason = f"discovery_failed: {type(e).__name__}"
        _discovery_failure_cache[cache_key] = (_now() + 60.0, reason)
        return [], reason

    if response.status_code in (401, 403):
        reason = (
            "auth_failed: the provider rejected this key "
            f"(HTTP {response.status_code})"
        )
        _discovery_failure_cache[cache_key] = (_now() + 60.0, reason)
        return [], reason
    if response.status_code == 429:
        reason = "rate_limited: provider throttled the list-models call"
        _discovery_failure_cache[cache_key] = (_now() + 30.0, reason)
        return [], reason
    if response.status_code != 200:
        reason = f"discovery_failed: HTTP {response.status_code}"
        _discovery_failure_cache[cache_key] = (_now() + 60.0, reason)
        return [], reason

    try:
        data = response.json()
    except ValueError:
        reason = "discovery_failed: provider returned non-JSON"
        _discovery_failure_cache[cache_key] = (_now() + 60.0, reason)
        return [], reason

    entries = data.get("models") if provider == "gemini" else data.get("data")
    if not isinstance(entries, list):
        entries = []

    models: list[DiscoveredModel] = []
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        parsed = _parse_model(provider, raw)
        if parsed is not None:
            models.append(parsed)

    _discovery_cache[cache_key] = (_now() + settings.MODEL_STATUS_TTL_S, models)
    _discovery_failure_cache.pop(cache_key, None)
    return models, None


def rank_candidates(
    provider: str, models: list[DiscoveredModel], denied: set[str]
) -> list[DiscoveredModel]:
    """Order candidates: preference group asc, numeric version desc,
    created desc (where the API gives one), id asc for determinism."""
    def sort_key(model: DiscoveredModel):
        group, _ = preference_group(provider, model.id)
        return (
            group,
            tuple(-part for part in (model.version or ())),
            -(model.created or 0),
            model.id,
        )

    return sorted(
        [m for m in models if m.chat_capable and m.id not in denied],
        key=sort_key,
    )


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
# The shared candidate walk (routing + Test button use THIS code path)
# --------------------------------------------------------------------------


async def walk_candidates(
    provider_name: str,
    prompt: str,
    kwargs: dict,
    api_key: str,
    first_model: Optional[str] = None,
    max_attempts: int = _MAX_CANDIDATE_ATTEMPTS,
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
            kwargs.get(kwarg_name)
            or getattr(provider_obj, "DEFAULT_MODEL", None)
            or ""
        )
        ordered = [fallback]

    steps: list[WalkStep] = []
    replacement_hint: Optional[str] = None
    tried: set[str] = set()
    kwarg_name = MODEL_KWARG.get(provider_name, "model")

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
        call_kwargs[kwarg_name] = model_id
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

        if error.startswith("model_not_found"):
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
        if error == "rate_limit_exceeded":
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
    max_attempts: int = _MAX_CANDIDATE_ATTEMPTS,
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

    kwarg_name = MODEL_KWARG.get(provider_name, "model")
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
        cached = _discovery_cache.get((key_fingerprint(api_key), provider_name))
        if cached and cached[0] > _now() and cached[1]:
            top = rank_candidates(
                provider_name,
                cached[1],
                denied_models(api_key, provider_name),
            )
            if top:
                initial_model = top[0].id

    call_kwargs = dict(kwargs)
    if initial_model:
        call_kwargs[kwarg_name] = initial_model
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
    error = response.error or ""
    if response.success or not error.startswith("model_not_found"):
        if response.success and initial_model:
            mark_status(api_key, provider_name, initial_model, "works")
        elif error == "rate_limit_exceeded" and initial_model:
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
            error=error,
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
        call_kwargs[kwarg_name] = model_id
        response = await provider_obj.generate(prompt, **call_kwargs)
        error = response.error or ""

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

        if error.startswith("model_not_found"):
            mark_status(api_key, provider_name, model_id, "denied")
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

        if error == "rate_limit_exceeded":
            mark_status(api_key, provider_name, model_id, "rate_limited")
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
        break  # non-model errors: let the executor fall through to the next provider

    return response, steps, initial_model
