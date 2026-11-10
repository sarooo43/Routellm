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

import time
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlsplit

import httpx

from app.config import settings
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


def choose_model(spec: ProviderSpec, available: Iterable[str]) -> str:
    """First preference the account actually offers, else its first chat model.

    Order matters and is asserted by tests: a *stable* default beats an
    alphabetical accident, and the hardcoded default still wins when it is
    available, so behaviour does not change for users it already worked for.
    """
    offered = [m for m in available]
    if not offered:
        return spec.default_model
    by_lower = {m.lower(): m for m in offered}
    for preferred in spec.preferred_models:
        if preferred.lower() in by_lower:
            return by_lower[preferred.lower()]
    for model in offered:
        if is_chat_capable(spec, model):
            return model
    return spec.default_model


@dataclass
class _CacheEntry:
    models: list[str]
    fetched_at: float
    source: str = "discovered"

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
        import hashlib

        digest = hashlib.sha256((api_key or "").encode()).hexdigest()[:16]
        return provider, digest

    def peek(self, provider: str, api_key: Optional[str]) -> Optional[list[str]]:
        """Cached list if fresh, else None. Never performs I/O."""
        entry = self._cache.get(self._key(provider, api_key))
        if entry and entry.fresh(time.monotonic(), self._ttl):
            return list(entry.models)
        return None

    def store(self, provider: str, api_key: Optional[str], models: list[str], source: str = "discovered") -> None:
        self._cache[self._key(provider, api_key)] = _CacheEntry(
            models=list(models), fetched_at=time.monotonic(), source=source
        )

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
        if not force_refresh:
            cached = self.peek(spec.name, api_key)
            if cached is not None:
                return cached, "cache"
            # Cold process (first request after a restart): a database row
            # written less than TTL ago is as good as a fresh fetch and costs
            # no provider call.
            persisted = await self._load_persisted(spec.name, api_key)
            if persisted:
                self.store(spec.name, api_key, persisted, source="cache")
                return persisted, "db_cache"

        models = await self._fetch(spec, api_key)
        if not models:
            return [], "unavailable"
        self.store(spec.name, api_key, models)
        await self._save_persisted(spec.name, api_key, models)
        return models, "discovered"

    # ---- persistence: advisory only, never fatal -------------------------

    async def _load_persisted(self, provider: str, api_key: Optional[str]) -> list[str]:
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
                return []
            if (time.time() - float(row.fetched_at or 0.0)) >= self._ttl:
                return []
            loaded = json.loads(row.models_json)
            return [m for m in loaded if isinstance(m, str)]
        except Exception as exc:  # noqa: BLE001 - cache is advisory
            logger.debug("model_cache_db_read_skipped", provider=provider, error=type(exc).__name__)
            return []

    async def _save_persisted(self, provider: str, api_key: Optional[str], models: list[str]) -> None:
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
                payload = json.dumps(models[:200])
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

    async def _fetch(self, spec: ProviderSpec, api_key: Optional[str]) -> list[str]:
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
            return []

        if response.status_code != 200:
            logger.info(
                "model_discovery_rejected",
                provider=spec.name,
                status=response.status_code,
                # Never log the body here: it can echo credential hints.
            )
            return []

        try:
            data = response.json()
        except Exception:  # pragma: no cover - non-JSON 200
            return []

        ids = _extract_model_ids(spec, data)
        logger.info(
            "model_discovery_ok",
            provider=spec.name,
            models=len(ids),
            sample=ids[:3],
        )
        return ids


def _extract_model_ids(spec: ProviderSpec, data: dict) -> list[str]:
    """Read both list shapes: OpenAI ``{data:[{id}]}`` and Gemini ``{models:[{name}]}``."""
    raw: list[str] = []
    if isinstance(data.get("data"), list):
        raw = [str(item.get("id", "")) for item in data["data"] if isinstance(item, dict)]
    elif isinstance(data.get("models"), list):
        for item in data["models"]:
            if not isinstance(item, dict):
                continue
            # Gemini: "models/gemini-2.0-flash" plus supportedGenerationMethods.
            methods = item.get("supportedGenerationMethods")
            if isinstance(methods, list) and methods and "generateContent" not in methods:
                continue
            name = str(item.get("name", ""))
            if name.startswith("models/"):
                name = name[len("models/"):]
            raw.append(name)

    cleaned = [m for m in raw if m and is_chat_capable(spec, m)]
    # Preserve provider order but drop duplicates.
    seen: set[str] = set()
    unique: list[str] = []
    for model in cleaned:
        if model not in seen:
            seen.add(model)
            unique.append(model)
    return unique


#: Process-wide cache. Cheap to construct, so tests build their own.
model_catalog = ModelCatalog()


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
