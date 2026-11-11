"""Per-provider wire facts, and discovery of which models actually exist.

The bug this replaces: each provider class hardcoded ONE model string
(``llama-3.3-70b-versatile``, ``gemini-2.0-flash``, ``deepseek-coder``) and
never checked it against the account. When a model was retired or not enabled
for a user's key, routing returned 404 and the user was told their key was
bad.

Two ideas fix that:

* **URLs are built from parts**, once, here — so a unit test can assert the
  exact string and a reviewer can see there is no double slash or duplicated
  ``/v1``.
* **The model is discovered**, not assumed: ask the provider's list-models
  endpoint with the caller's key, keep the chat-capable ones, and prefer our
  ordered list. The result is cached for an hour and refreshed when a request
  comes back ``model_not_found``.

Discovery is cached twice: in this module's memory for the running process, and
in the database (a one-row-per-(provider, key-fingerprint) table) so a restart
does not put a live list-models call on the critical path of the first prompt.

Discovery is best-effort by design: if the list endpoint fails, routing must
still work with the default, so every failure here degrades to "use the
configured default" rather than raising. Persistence is best-effort too — a
missing database is not an error, it just means no warm cache.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlsplit

import httpx

from app.config import MODEL_PREFERENCE_GROUPS, MODEL_TIER_TABLE, settings
from app.utils.logger import get_logger

logger = get_logger(__name__)

#: How long a discovered model list is trusted. Overridable with
#: ``MODEL_CACHE_TTL_SECONDS`` (read below, after the settings import).
MODEL_CACHE_TTL_S = 3600.0
#: Timeout for the list-models call. Discovery must never delay a real route
#: for long: if it is slow, we would rather use the default.
DISCOVERY_TIMEOUT_S = 8.0


def _configured_ttl() -> float:
    """Settings override, clamped to something sane (0 disables the cache)."""
    try:
        value = float(settings.MODEL_CACHE_TTL_SECONDS)
    except (TypeError, ValueError):
        return MODEL_CACHE_TTL_S
    return max(0.0, value)


@dataclass(frozen=True)
class ProviderSpec:
    """Everything needed to build a request and enumerate models."""

    name: str
    #: Base URL, no trailing slash.
    base_url: str
    #: Path for a chat completion, relative to base_url.
    chat_path: str
    #: Path for the model list, relative to base_url.
    models_path: str
    #: Model used when discovery is unavailable or disabled.
    default_model: str
    #: Ordered preference. First entry that the account actually offers wins.
    preferred_models: tuple[str, ...]
    #: Substrings that disqualify a model from chat routing (embeddings, audio,
    #: moderation, speech...). Matched case-insensitively anywhere in the id.
    non_chat_markers: tuple[str, ...] = (
        "embedding",
        "embed",
        "whisper",
        "tts",
        "audio",
        "image",
        "vision-preview",
        "guard",
        "moderation",
        "rerank",
        "aqa",
    )
    #: True when the model name belongs IN the URL (Gemini), not in the body.
    model_in_path: bool = False
    #: Prefix a discovered id needs when placed in the path (Gemini: "models/").
    model_path_prefix: str = ""

    def chat_url(self, model: str) -> str:
        """Exact chat URL. The single place where the string is composed.

        Gemini puts the model in the path:
            {base}/v1beta/models/{model}:generateContent
        Everything else posts to a fixed path and names the model in the body.
        Composing this in one function is what makes "no double slash, no
        duplicated /v1, no missing models/" an assertion instead of a hope.
        """
        if self.model_in_path:
            path = self.chat_path
            # Accept an id that already carries the prefix, and never emit it twice.
            bare = model[len(self.model_path_prefix):] if model.startswith(self.model_path_prefix) else model
            return f"{self.base_url}{path}{bare}:generateContent"
        return f"{self.base_url}{self.chat_path}"

    def models_url(self) -> str:
        return f"{self.base_url}{self.models_path}"

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).netloc


#: The wire facts, verified by unit tests that assert the built URLs verbatim.
PROVIDER_SPECS: dict[str, ProviderSpec] = {
    "groq": ProviderSpec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        chat_path="/chat/completions",
        models_path="/models",
        default_model="llama-3.3-70b-versatile",
        preferred_models=(
            "llama-3.3-70b-versatile",
            "llama-3.1-8b-instant",
            "openai/gpt-oss-120b",
            "openai/gpt-oss-20b",
            "qwen/qwen3-32b",
            "meta-llama/llama-4-scout-17b-16e-instruct",
            "gemma2-9b-it",
        ),
    ),
    "gemini": ProviderSpec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com",
        chat_path="/v1beta/models/",
        models_path="/v1beta/models",
        default_model="gemini-2.0-flash",
        preferred_models=(
            "gemini-2.0-flash",
            "gemini-2.0-flash-lite",
            "gemini-1.5-flash",
            "gemini-1.5-flash-8b",
            "gemini-1.5-pro",
        ),
        non_chat_markers=(
            "embedding",
            "embed",
            "aqa",
            "imagen",
            "veo",
            "tts",
            "native-audio",
            "thinking",
        ),
        model_in_path=True,
        model_path_prefix="models/",  # validated: never doubled into the URL
    ),
    "deepseek": ProviderSpec(
        name="deepseek",
        base_url="https://api.deepseek.com",
        chat_path="/v1/chat/completions",
        models_path="/v1/models",
        default_model="deepseek-chat",
        preferred_models=("deepseek-chat", "deepseek-reasoner"),
        non_chat_markers=("embedding", "embed"),
    ),
}


def spec_for(provider: str) -> Optional[ProviderSpec]:
    return PROVIDER_SPECS.get(provider)


def is_chat_capable(spec: ProviderSpec, model_id: str) -> bool:
    """Filter out embeddings, audio, image and other non-chat endpoints."""
    lowered = model_id.lower()
    return not any(marker in lowered for marker in spec.non_chat_markers)


def key_fingerprint(api_key: str) -> str:
    """Stable, non-reversible cache key for one upstream provider key.

    Only the first 16 hex chars of a SHA-256 are used — enough to scope caches
    per key, useless for reconstructing the secret. The single fingerprint
    implementation for every cache and DB row in the system.
    """
    return hashlib.sha256((api_key or "").encode("utf-8")).hexdigest()[:16]


def parse_version_tuple(model_id: str) -> Optional[tuple[int, ...]]:
    """Parse the FIRST numeric version group in an id as numbers.

    gemini-3.8-flash -> (3, 8); gemini-2.0-flash -> (2, 0);
    gemini-10.0-flash -> (10, 0) so 10.0 > 9.0 numerically, never lexically.
    Returns None when the id carries no version.
    """
    match = re.search(r"(?<![\w.])(\d+(?:\.\d+)*)", (model_id or "").lower())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def preference_group(provider: str, model_id: str) -> int:
    """Index of the first matching preference group from owner-editable config.

    ``MODEL_PREFERENCE_GROUPS[provider]`` is an ordered list of
    ``{match, exclude, note}`` patterns; a model lands in the first group
    whose ``match`` substring it contains and none of whose ``exclude``
    substrings it does. The final ``""`` group is the catch-all. Returns
    ``len(groups)`` when the provider has no table.
    """
    lowered = (model_id or "").lower()
    groups = MODEL_PREFERENCE_GROUPS.get(provider)
    if not groups:
        return 0
    for index, group in enumerate(groups):
        match = group.get("match", "")
        if match and match not in lowered:
            continue
        if any(excl in lowered for excl in group.get("exclude", [])):
            continue
        return index
    return len(groups)


def estimated_tier(provider: str, model_id: str) -> str:
    """Static tier ESTIMATE from owner-editable config.

    Provider list-models responses expose NO pricing, so "free"/"paid" is an
    estimate and every caller must label it ``tier_source="estimated"``.
    Returns "unknown" when nothing matches — never a guess dressed as data.
    """
    lowered = (model_id or "").lower()
    for entry in MODEL_TIER_TABLE.get(provider, []):
        match = entry.get("match", "")
        if not match or match in lowered:
            return entry.get("tier", "unknown")
    return "unknown"


def rank_candidate_ids(
    spec: ProviderSpec,
    ids: Iterable[str],
    created: Optional[dict[str, int]] = None,
) -> list[str]:
    """THE model ordering for the whole system (choose_model + candidate walk).

    Order: owner-editable preference group, then the spec's static
    ``preferred_models`` order (a tiebreak, so legacy deployments keep their
    documented choice), then numeric version DESC (3.8 > 2.5 > 2.0, and
    10.0 > 9.0 — parsed as numbers, never compared as strings), then the
    provider's ``created`` timestamp DESC where the API gives one, then the
    id for determinism. Non-chat ids are dropped; duplicates collapse
    case-insensitively.
    """
    created = created or {}
    preferred_index = {m.lower(): i for i, m in enumerate(spec.preferred_models)}

    def sort_key(model_id: str):
        group = preference_group(spec.name, model_id)
        pref = preferred_index.get(model_id.lower(), len(spec.preferred_models))
        version = parse_version_tuple(model_id)
        return (
            group,
            pref,
            tuple(-part for part in (version or ())),
            -created.get(model_id, 0),
            model_id,
        )

    seen: set[str] = set()
    unique: list[str] = []
    for model_id in ids:
        lowered = model_id.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        unique.append(model_id)
    return sorted(
        [m for m in unique if is_chat_capable(spec, m)],
        key=sort_key,
    )


def choose_model(spec: ProviderSpec, available: Iterable[str]) -> str:
    """First candidate in the shared ranking the account actually offers.

    Delegates to :func:`rank_candidate_ids` so routing, the Test connection
    button and the model dropdown CANNOT disagree about order. Order matters
    and is asserted by tests: a *stable* default beats an alphabetical
    accident, and behaviour does not change for users it already worked for.
    """
    ranked = rank_candidate_ids(spec, list(available))
    if not ranked:
        return spec.default_model
    return ranked[0]


@dataclass
class _CacheEntry:
    models: list[str]
    fetched_at: float
    source: str = "discovered"
    #: Wire facts per model id (display name, created, context window...),
    #: captured at discovery time. Missing/empty for legacy cache rows.
    meta: dict[str, dict] = field(default_factory=dict)

    def fresh(self, now: float, ttl_s: float = MODEL_CACHE_TTL_S) -> bool:
        return (now - self.fetched_at) < ttl_s


class ModelCatalog:
    """In-memory model discovery cache, keyed by (provider, key fingerprint).

    Keyed by a *hash* of the credential, never the credential: two users with
    different accounts can see different model lists, and we must not keep a
    plaintext key in a dict for an hour.
    """

    @staticmethod
    def session_maker():
        """Indirection for the persistence layer (tests replace this)."""
        from app.db.session import get_session_maker

        return get_session_maker()

    def __init__(
        self, ttl_s: Optional[float] = None, timeout_s: float = DISCOVERY_TIMEOUT_S
    ):
        self._ttl = _configured_ttl() if ttl_s is None else ttl_s
        self._timeout = timeout_s
        self._cache: dict[tuple[str, str], _CacheEntry] = {}

    @staticmethod
    def _key(provider: str, api_key: Optional[str]) -> tuple[str, str]:
        return provider, key_fingerprint(api_key or "")

    def peek(self, provider: str, api_key: Optional[str]) -> Optional[list[str]]:
        """Cached list if fresh, else None. Never performs I/O."""
        entry = self._cache.get(self._key(provider, api_key))
        if entry and entry.fresh(time.monotonic(), self._ttl):
            return list(entry.models)
        return None

    def store(
        self,
        provider: str,
        api_key: Optional[str],
        models: list[str],
        source: str = "discovered",
        meta: Optional[dict[str, dict]] = None,
    ) -> None:
        self._cache[self._key(provider, api_key)] = _CacheEntry(
            models=list(models),
            fetched_at=time.monotonic(),
            source=source,
            meta=meta or {},
        )

    def peek_meta(self, provider: str, api_key: Optional[str]) -> dict[str, dict]:
        """Wire facts for the cached list, or {} when nothing is cached."""
        entry = self._cache.get(self._key(provider, api_key))
        if entry and entry.fresh(time.monotonic(), self._ttl):
            return dict(entry.meta)
        return {}

    def invalidate(self, provider: str, api_key: Optional[str] = None) -> None:
        """Drop one in-memory entry, or every entry for a provider."""
        if api_key is not None:
            self._cache.pop(self._key(provider, api_key), None)
            return
        for key in [k for k in self._cache if k[0] == provider]:
            self._cache.pop(key, None)

    async def invalidate_everywhere(
        self, provider: str, api_key: Optional[str] = None
    ) -> None:
        """Forget BOTH tiers.

        Used after ``model_not_found``. Clearing only the memory tier is not
        enough: the persisted row would be read back on the next call and the
        caller would retry the same bad model — the exact loop this feature is
        meant to break.
        """
        self.invalidate(provider, api_key)
        try:
            from sqlalchemy import delete

            from app.models.model_cache import DiscoveredModelCacheDB

            maker = self.session_maker()
            async with maker() as session:
                statement = delete(DiscoveredModelCacheDB).where(
                    DiscoveredModelCacheDB.provider == provider
                )
                if api_key is not None:
                    _, fingerprint = self._key(provider, api_key)
                    statement = statement.where(
                        DiscoveredModelCacheDB.key_fingerprint == fingerprint
                    )
                await session.execute(statement)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - cache is advisory
            logger.debug(
                "model_cache_db_invalidate_skipped",
                provider=provider,
                error=type(exc).__name__,
            )

    def clear(self) -> None:  # pragma: no cover - test hygiene
        self._cache.clear()

    async def list_records(
        self,
        spec: ProviderSpec,
        api_key: Optional[str],
        *,
        force_refresh: bool = False,
    ) -> tuple[list[dict], str]:
        """Discovered chat models WITH their wire facts, and the cache source.

        Same discovery call and cache as :meth:`list_models` — there is no
        second fetch. Each record is a tolerant dict: ``id`` always present;
        ``display_name``, ``created``, ``context_window``, ``max_output``,
        ``capabilities`` present when the provider exposed them (Gemini has no
        ``created``; Groq has no capability field).
        """
        ids, meta, source = await self._discover(spec, api_key, force_refresh=force_refresh)
        records = []
        for model_id in ids:
            record = {"id": model_id}
            record.update(meta.get(model_id, {}))
            records.append(record)
        return records, source

    async def list_models(
        self,
        spec: ProviderSpec,
        api_key: Optional[str],
        *,
        force_refresh: bool = False,
    ) -> tuple[list[str], str]:
        """Available chat model ids, and where they came from.

        Returns ``([], "unavailable")`` when discovery fails — the caller falls
        back to the configured default rather than failing the route.
        """
        ids, _meta, source = await self._discover(spec, api_key, force_refresh=force_refresh)
        return ids, source

    async def _discover(
        self,
        spec: ProviderSpec,
        api_key: Optional[str],
        *,
        force_refresh: bool = False,
    ) -> tuple[list[str], dict[str, dict], str]:
        """The single discovery path: memory cache → DB cache → live fetch."""
        if not force_refresh:
            entry = self._cache.get(self._key(spec.name, api_key))
            if entry and entry.fresh(time.monotonic(), self._ttl):
                return list(entry.models), dict(entry.meta), "cache"
            # Cold process (first request after a restart): a database row
            # written less than TTL ago is as good as a fresh fetch and costs
            # no provider call.
            ids, meta = await self._load_persisted(spec.name, api_key)
            if ids:
                self.store(spec.name, api_key, ids, source="cache", meta=meta)
                return ids, meta, "db_cache"

        ids, meta = await self._fetch(spec, api_key)
        if not ids:
            return [], {}, "unavailable"
        self.store(spec.name, api_key, ids, meta=meta)
        await self._save_persisted(spec.name, api_key, ids, meta)
        return ids, meta, "discovered"

    # ---- persistence: advisory only, never fatal -------------------------

    async def _load_persisted(self, provider: str, api_key: Optional[str]) -> tuple[list[str], dict[str, dict]]:
        try:
            import json

            from sqlalchemy import select

            from app.models.model_cache import DiscoveredModelCacheDB

            _, fingerprint = self._key(provider, api_key)
            maker = self.session_maker()
            async with maker() as session:
                row = (
                    await session.execute(
                        select(DiscoveredModelCacheDB).where(
                            DiscoveredModelCacheDB.provider == provider,
                            DiscoveredModelCacheDB.key_fingerprint == fingerprint,
                        )
                    )
                ).scalars().first()
            if row is None:
                return [], {}
            if (time.time() - float(row.fetched_at or 0.0)) >= self._ttl:
                return [], {}
            ids, meta = _load_cached_payload(row.models_json)
            return ids, meta
        except Exception as exc:  # noqa: BLE001 - cache is advisory
            logger.debug("model_cache_db_read_skipped", provider=provider, error=type(exc).__name__)
            return [], {}

    async def _save_persisted(
        self, provider: str, api_key: Optional[str], ids: list[str], meta: dict[str, dict]
    ) -> None:
        try:
            from sqlalchemy import select

            from app.models.model_cache import DiscoveredModelCacheDB

            fingerprint = key_fingerprint(api_key or "")
            maker = self.session_maker()
            async with maker() as session:
                row = (
                    await session.execute(
                        select(DiscoveredModelCacheDB).where(
                            DiscoveredModelCacheDB.provider == provider,
                            DiscoveredModelCacheDB.key_fingerprint == fingerprint,
                        )
                    )
                ).scalars().first()
                # v2 payload keeps the wire facts so the explorer UI survives a
                # restart; old rows (a bare list of ids) stay readable forever.
                payload = json.dumps(
                    {"v": 2, "models": [_record_for_storage(m, meta.get(m, {})) for m in ids[:MAX_CACHED_MODELS]]}
                )
                if row is None:
                    session.add(
                        DiscoveredModelCacheDB(
                            provider=provider,
                            key_fingerprint=fingerprint,
                            models_json=payload,
                            fetched_at=time.time(),
                        )
                    )
                else:
                    row.models_json = payload
                    row.fetched_at = time.time()
                    session.add(row)
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - never break discovery
            logger.debug("model_cache_db_write_skipped", provider=provider, error=type(exc).__name__)

    async def _fetch(self, spec: ProviderSpec, api_key: Optional[str]) -> tuple[list[str], dict[str, dict]]:
        url = spec.models_url()
        headers = {}
        if api_key and spec.name == "gemini":
            headers["x-goog-api-key"] = api_key
        elif api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(url, headers=headers)
        except Exception as exc:
            logger.info(
                "model_discovery_failed",
                provider=spec.name,
                host=spec.host,
                error=type(exc).__name__,
            )
            return [], {}

        if response.status_code != 200:
            logger.info(
                "model_discovery_rejected",
                provider=spec.name,
                status=response.status_code,
                # Never log the body here: it can echo credential hints.
            )
            _capture_provider_shape(spec.name, "list_models", response)
            return [], {}

        try:
            data = response.json()
        except Exception:  # pragma: no cover - non-JSON 200
            return [], {}

        _capture_provider_shape(spec.name, "list_models", response, data=data)
        ids, meta = _extract_model_ids(spec, data)
        logger.info(
            "model_discovery_ok",
            provider=spec.name,
            models=len(ids),
            sample=ids[:3],
        )
        return ids, meta


#: Cap on how many models one cache row keeps. A runaway list (500+ models,
#: Step 3 hardening) must not bloat the DB row or the UI.
MAX_CACHED_MODELS = 500

#: Wire-fact fields worth keeping per model, whitelisted by NAME. Values are
#: sanitized (ints kept, strings truncated) — a hostile provider response can
#: never smuggle long text into the cache row.
_META_FIELDS = (
    "display_name",
    "created",
    "context_window",
    "max_output",
    "max_completion_tokens",
    "capabilities",
)


def _record_for_storage(model_id: str, meta: dict) -> dict:
    record = {"id": model_id}
    for field_name in _META_FIELDS:
        value = meta.get(field_name)
        if isinstance(value, int):
            record[field_name] = value
        elif isinstance(value, str):
            record[field_name] = value[:80]
        elif isinstance(value, list):
            record[field_name] = [str(item)[:40] for item in value[:10]]
    return record


def _load_cached_payload(models_json: str) -> tuple[list[str], dict[str, dict]]:
    """Read a persisted cache row. Tolerant of EVERY historical shape.

    * v2 (current): ``{"v": 2, "models": [{"id": ..., "created": ...}]}`` —
      ids plus wire facts.
    * v1 (PR #15): a bare JSON list of id strings — ids only, no facts.
    * Anything unreadable: empty list, never an exception.
    """
    import json

    try:
        loaded = json.loads(models_json)
    except Exception:
        return [], {}
    ids: list[str] = []
    meta: dict[str, dict] = {}
    items: list = []
    if isinstance(loaded, dict) and isinstance(loaded.get("models"), list):
        items = loaded["models"]
    elif isinstance(loaded, list):
        items = loaded
    for item in items:
        if isinstance(item, str):
            if item:
                ids.append(item)
        elif isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
            if not model_id:
                continue
            ids.append(model_id)
            facts = {k: v for k, v in item.items() if k != "id"}
            if facts:
                meta[model_id] = facts
    # Duplicate ids collapse (first occurrence wins); cap runaway lists.
    unique: list[str] = []
    seen: set[str] = set()
    for model_id in ids[:MAX_CACHED_MODELS]:
        if model_id in seen:
            continue
        seen.add(model_id)
        unique.append(model_id)
    return unique, {k: meta[k] for k in unique if k in meta}


def _extract_model_ids(spec: ProviderSpec, data: dict) -> tuple[list[str], dict[str, dict]]:
    """Read both list shapes: OpenAI ``{data:[{id}]}`` and Gemini ``{models:[{name}]}``.

    Tolerant by contract (Step 3 hardening): a non-dict body, an empty list,
    missing fields, unexpected types or duplicate ids degrade to fewer/empty
    results — a parse failure NEVER raises into the endpoint.
    """
    ids: list[str] = []
    meta: dict[str, dict] = {}
    if not isinstance(data, dict):
        return [], {}
    entries: list = []
    if isinstance(data.get("data"), list):
        entries = data["data"]
    elif isinstance(data.get("models"), list):
        entries = data["models"]

    for item in entries[:MAX_CACHED_MODELS]:
        if isinstance(item, str):
            # Some OpenAI-compatible providers list bare strings.
            name = item.removeprefix("models/").strip()
            if name:
                ids.append(name)
            continue
        if not isinstance(item, dict):
            continue
        # Gemini: "models/gemini-2.0-flash" plus supportedGenerationMethods.
        methods = item.get("supportedGenerationMethods")
        if isinstance(methods, list) and methods and "generateContent" not in methods:
            continue
        name = str(item.get("id") or item.get("name") or "")
        if not isinstance(item.get("id"), str) and not isinstance(item.get("name"), str):
            # A numeric/object id is an unexpected type: drop the entry rather
            # than stringify garbage into the cache.
            continue
        if name.startswith("models/"):
            name = name[len("models/"):]
        name = name.strip()
        if not name:
            continue
        ids.append(name)
        facts: dict = {}
        display = item.get("displayName") or item.get("display_name")
        if isinstance(display, str) and display:
            facts["display_name"] = display[:80]
        created = item.get("created")
        if isinstance(created, int) and not isinstance(created, bool):
            facts["created"] = created
        for key, alias in (("context_window", "context_window"), ("inputTokenLimit", "context_window")):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                facts.setdefault(alias, value)
        for key, alias in (
            ("max_completion_tokens", "max_output"),
            ("outputTokenLimit", "max_output"),
        ):
            value = item.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                facts.setdefault(alias, value)
        if isinstance(methods, list) and methods:
            facts["capabilities"] = [str(m)[:40] for m in methods[:10]]
        if facts:
            meta[name] = facts

    cleaned: list[str] = []
    seen: set[str] = set()
    for name in ids:
        if name in seen:
            continue
        seen.add(name)
        if is_chat_capable(spec, name):
            cleaned.append(name)
    return cleaned, {k: meta[k] for k in cleaned if k in meta}


#: Process-wide cache. Cheap to construct, so tests build their own.
model_catalog = ModelCatalog()


# --------------------------------------------------------------------------
# Debug capture (OFF by default) — real wire shapes, sanitized
# --------------------------------------------------------------------------


def _capture_provider_shape(provider: str, kind: str, response, data=None) -> None:
    """Append a sanitized shape sample to data/provider_shapes/<provider>.json.

    Enabled only by ROUTELLM_CAPTURE_PROVIDER_SHAPES=true. Writes FIELD NAMES
    plus a truncated, key-REDACTED sample of the body so an operator can send
    us the real wire shape without sharing a key. Every write is best-effort:
    a missing or read-only data directory simply disables the capture. Tested
    to never contain a credential.
    """
    if not settings.ROUTELLM_CAPTURE_PROVIDER_SHAPES:
        return
    try:
        from app.local import data_dir

        target_dir = data_dir(create=True) / "provider_shapes"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{provider}.json"

        from app.providers.outcomes import redact

        try:
            body = response.text or ""
        except Exception:  # pragma: no cover
            body = ""
        entry = {
            "captured_at": time.time(),
            "kind": kind,
            "status": getattr(response, "status_code", None),
            "body_field_names": sorted(
                _collect_field_names(data if data is not None else body)
            ),
            "body_sample": redact(" ".join(body.split())[:1000]),
        }
        existing = []
        if target.is_file():
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    existing = loaded
            except Exception:
                existing = []
        existing.append(entry)
        target.write_text(json.dumps(existing[-50:], indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001 - capture must never break discovery
        logger.debug("provider_shape_capture_failed", provider=provider, kind=kind)


def _collect_field_names(payload) -> list[str]:
    """Field NAMES only (never values), recursing shallowly."""
    names: list[str] = []

    def walk(node, depth: int) -> None:
        if depth > 4:
            return
        if isinstance(node, dict):
            for key, value in node.items():
                names.append(str(key))
                if isinstance(value, (dict, list)):
                    walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node[:3]:
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return ["<non-json body>"]
    walk(payload, 0)
    return sorted(set(names))


async def resolve_model(
    provider: str,
    api_key: Optional[str],
    *,
    override: Optional[str] = None,
    force_refresh: bool = False,
) -> tuple[str, list[str], str]:
    """The model to use, the discovery list, and where the choice came from.

    Precedence: explicit override (env or per-request) > discovered preference >
    static default. Overrides are NOT validated against the account: the user
    may know about a model the list endpoint does not expose, and refusing to
    try would be worse than letting them.
    """
    spec = spec_for(provider)
    if spec is None:
        return (override or ""), [], "unsupported"

    if override:
        models = model_catalog.peek(provider, api_key) or []
        return override, models, "override"

    models, source = await model_catalog.list_models(spec, api_key, force_refresh=force_refresh)
    if not models:
        return spec.default_model, [], "default"
    return choose_model(spec, models), models, source
