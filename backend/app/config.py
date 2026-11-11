from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # LOCAL-FIRST: leave DATABASE_URL empty to get SQLite in the data dir.
    # Set it (postgres:// or sqlite+aiosqlite://) to override.
    DATABASE_URL: Optional[str] = None
    # LOCAL-FIRST: leave REDIS_URL empty to get the in-process store.
    REDIS_URL: Optional[str] = None
    # Directory for SQLite, the generated encryption key and logs.
    # Defaults to <repo>/data, falling back to the OS user-data dir.
    ROUTELLM_DATA_DIR: str = ""
    # LOCAL-FIRST: when false (the default) /api/v1/route and
    # /v1/chat/completions work without X-API-Key and usage is attributed to a
    # default local identity. Set true to require keys on every protected route.
    REQUIRE_API_KEY: bool = False
    # Serve the built frontend (frontend/dist) from this process so the app is
    # a single URL. Disable for a separate Vite dev server.
    SERVE_FRONTEND: bool = True
    # Optional explicit path to the built frontend directory.
    FRONTEND_DIST_DIR: str = ""
    GROQ_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    DEEPSEEK_API_KEY: Optional[str] = None
    # Model overrides. Empty = DISCOVER it: ask the provider which models this
    # key can actually use and pick the first from an ordered preference list.
    # Set one of these only when you need a specific model, e.g. after a
    # provider retires the default (that failure used to surface as a bogus
    # "your key is invalid" message).
    GROQ_MODEL: str = ""
    GEMINI_MODEL: str = ""
    DEEPSEEK_MODEL: str = ""
    # How long a discovered model list is cached before it is re-fetched.
    MODEL_CACHE_TTL_SECONDS: int = 3600
    # Fernet key used to encrypt per-caller provider API keys (BYOK) at rest.
    # Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # When unset, provider-key writes and BYOK routing fail closed with 503.
    PROVIDER_KEY_ENCRYPTION_KEY: Optional[str] = None
    # Comma-separated browser origins allowed to call this backend cross-origin,
    # e.g. CORS_ORIGINS=http://localhost:5173,https://routellm.example.workers.dev
    # (the Cloudflare Worker proxies same-origin server-to-server, so this only
    # matters for clients that set VITE_API_BASE_URL directly at the backend).
    CORS_ORIGINS: str = ""
    # Host-header allow-list for direct HTTP access (DNS-rebinding defence).
    # The middleware ALWAYS accepts the loopback names (localhost, 127.0.0.1,
    # ::1/[::1]) plus the HOST this process binds. A deployed public hostname
    # (e.g. your Space's *.hf.space, or a tunnel URL) must be listed here,
    # comma-separated. Set "*" to disable the check entirely — only when the
    # service sits behind a proxy you fully control.
    ALLOWED_HOSTS: str = ""
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    ROUTE_RATE_LIMIT: str = "30/minute"
    # Fault-injection endpoints + executor fake failures (a demo feature).
    # On by default because this tool runs locally, on your machine, behind
    # 127.0.0.1 — the risk that justified keeping it off on a public Space does
    # not apply. Set CHAOS_MODE_ENABLED=false to disable.
    # See core/chaos.py — a Chaos Monkey-style demo control for the dashboard.
    CHAOS_MODE_ENABLED: bool = True
    # EXPERIMENTAL: ML-assisted routing (learned capability scores blended
    # with the rule-based table, 60/40). Off until the router model is
    # trained (scripts/train_router_model.py) AND validated. Requires the
    # model file backend/app/ml_models/router_classifier.pkl to exist.
    ENABLE_ML_ROUTING: bool = False
    # LOCAL-FIRST: a MOCK provider (name "mock", model "mock-demo-v1") that
    # answers without any network call, so a fresh clone can show routing,
    # fallback, latency and cost behaviour with no key and no spend. Every
    # response is prefixed with a DEMO marker and demo traffic is excluded from
    # reports and benchmarks. It is never used while a real provider key is
    # configured. Set false to remove it entirely.
    DEMO_MODE_ENABLED: bool = True
    # Simulated upstream latency for the demo provider, in milliseconds.
    DEMO_LATENCY_MS: int = 350
    # Probability (0.0-1.0) the demo provider fails, to exercise fallback.
    DEMO_FAILURE_RATE: float = 0.0
    # Port to bind when the app is started as a script (run.py / hf_app.py).
    # 8000 for the local-first default; Hugging Face Spaces inject PORT=7860.
    PORT: int = 8000
    # LOCAL-FIRST SECURITY: loopback only. Binding to 0.0.0.0 exposes the
    # gateway (and your provider keys' spending power) to your whole network,
    # so it requires an explicit --host flag or HOST=0.0.0.0 plus a warning.
    HOST: str = "127.0.0.1"
    # Set to "1"/"true" to force the Hugging Face filesystem rules (no writes
    # inside the app dir) when testing a read-only deployment elsewhere.
    ROUTELLM_READ_ONLY_APP_DIR: str = ""
    # Optional explicit runtime directory override (logs/scratch files).
    ROUTELLM_RUNTIME_DIR: str = ""
    # Per-component budget for /health probes. Kept short so a sleeping
    # Space's health check cannot hang a load balancer.
    HEALTH_PROBE_TIMEOUT_S: float = 2.0
    # Optional per-provider .env model override for routing. Precedence for
    # routing: caller's pinned model, then this override, then Auto
    # (candidate walking over discovered models). Empty string = unset
    # (declared once above with the other provider settings).
    # Timeout for a provider's list-models call (discovery).
    MODEL_DISCOVERY_TIMEOUT_S: float = 8.0
    # How long discovered lists / per-model statuses (denied, rate_limited)
    # are trusted, in seconds. ~1 hour so a key whose cohort gains access to
    # a model is retested soon after the TTL expires.
    MODEL_STATUS_TTL_S: int = 3600
    # DEBUG CAPTURE (OFF by default): when true, sanitized field NAMES and a
    # truncated, key-redacted sample of every provider list-models response
    # and error body are appended to data/provider_shapes/<provider>.json so
    # an operator can report real wire shapes without sharing keys. Never
    # enable this on a shared deployment: the samples are still provider
    # response text.
    ROUTELLM_CAPTURE_PROVIDER_SHAPES: bool = False


settings = Settings()


# ==========================================================================
# Model discovery policy tables (STATIC, owner-editable).
#
# Provider list-models responses carry NO pricing/tier information (see
# docs in app/core/model_discovery.py), so "free/paid" is an ESTIMATE from
# these tables. Every tier the API reports is labeled tier_source="estimated"
# and unmatched models get "unknown" — never a guess dressed up as data.
#
# Versions are parsed as NUMBERS (3.8 > 2.5 > 2.0, and 10.0 > 9.0), never
# compared as strings.
# ==========================================================================

# Preference groups per provider, checked IN ORDER (first match wins).
# ``match`` is a case-insensitive substring; ``exclude`` lists substrings
# that push a model out of this (higher) group into the next one. The final
# ``""`` group is the catch-all.
MODEL_PREFERENCE_GROUPS: dict[str, list[dict]] = {
    "gemini": [
        # Newest plain "flash" first: gemini-3.8-flash beats 2.5-flash, but
        # flash-lite / preview / exp / thinking / image variants rank lower.
        {
            "match": "flash",
            "exclude": ["lite", "preview", "exp", "thinking", "image", "tts", "embedding"],
            "note": "plain flash models (no lite/preview/experimental suffix)",
        },
        {"match": "flash-lite", "exclude": ["preview", "exp"], "note": "flash-lite fallback"},
        {"match": "pro", "exclude": ["preview", "exp", "image", "tts"], "note": "pro fallback"},
        {"match": "", "exclude": [], "note": "anything else"},
    ],
    "groq": [
        {"match": "versatile", "exclude": ["preview", "exp"], "note": "versatile chat models"},
        {"match": "chat", "exclude": ["preview", "exp"], "note": "chat models"},
        {"match": "", "exclude": [], "note": "anything else"},
    ],
    "deepseek": [
        {"match": "chat", "exclude": [], "note": "chat models"},
        {"match": "reasoner", "exclude": [], "note": "reasoner fallback"},
        {"match": "", "exclude": [], "note": "anything else"},
    ],
}

# Non-chat / non-text model families. They are never routing candidates and
# are hidden from the model explorer (a hidden count is shown instead).
MODEL_EXCLUDE_PATTERNS: list[str] = [
    "embedding",
    "embed",
    "tts",
    "whisper",
    "guard",
    "moderation",
    "image",
    "imagen",
    "veo",
    "video",
    "audio",
    "aqa",
    "rank",
]

# Static tier estimates, first matching pattern wins, per provider.
# THESE ARE ESTIMATES — the provider APIs do not expose pricing in the
# list-models response. Edit freely to match your accounts' reality.
MODEL_TIER_TABLE: dict[str, list[dict]] = {
    "gemini": [
        {"match": "flash", "tier": "free"},
        {"match": "pro", "tier": "paid"},
    ],
    # Groq serves open-weight models with a generous free tier.
    "groq": [{"match": "", "tier": "free"}],
    # DeepSeek's API has no free tier.
    "deepseek": [{"match": "", "tier": "paid"}],
}


def chaos_mode_enabled() -> bool:
    """Resolve the effective Chaos Mode switch.

    An explicit CHAOS_MODE_ENABLED in the environment always wins. When unset
    (None), the local-first default is ON (fault injection is the point of
    the demo and is harmless on one machine); on Hugging Face the default is
    OFF, exactly matching the pre-local-first behavior.
    """
    if settings.CHAOS_MODE_ENABLED is not None:
        return settings.CHAOS_MODE_ENABLED
    from app.runtime import running_on_huggingface

    return not running_on_huggingface()


class _CorsOrigins:
    """Module-level helper so tests can inspect the parsed allowlist."""

    @staticmethod
    def parse(raw: str) -> list[str]:
        origins = [o.strip().rstrip("/") for o in (raw or "").split(",") if o.strip()]
        return origins


def normalize_redis_url(raw: Optional[str]) -> Optional[str]:
    """Return a Redis DSN that the async client accepts, or None when unset.

    ``rediss://`` (TLS, what Upstash hands out) is passed through untouched —
    ``redis.asyncio.from_url`` already selects the TLS connection class from
    the scheme. The only rewrite is the legacy/managed-provider habit of using
    ``redis://`` against a TLS-only host, which is left alone *on purpose*:
    silently upgrading the scheme would turn a clear connection error into a
    confusing TLS handshake failure. See docs/deploy/HUGGINGFACE_AND_MANAGED.md troubleshooting.
    """
    if not raw:
        return None
    url = raw.strip()
    if not url:
        return None
    if url.startswith(("redis://", "rediss://", "unix://")):
        return url
    raise ValueError(
        "REDIS_URL must start with redis://, rediss:// or unix:// "
        f"(got {url.split(':', 1)[0]!r})"
    )


def redis_uses_tls(raw: Optional[str]) -> bool:
    """True for rediss:// (TLS) DSNs — used by the startup log."""
    return bool(raw) and str(raw).strip().lower().startswith("rediss://")


def cors_origin_list(settings_obj: Optional[Settings] = None) -> list[str]:
    """Browser-origin allowlist for CORSMiddleware. Falls back to the Vite
    dev origins when CORS_ORIGINS is unset so local development needs zero
    configuration; production origins must be set explicitly."""
    s = settings_obj or settings
    origins = _CorsOrigins.parse(s.CORS_ORIGINS)
    if origins:
        return origins
    return [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
    ]


def allowed_host_list(settings_obj: Optional[Settings] = None) -> list[str]:
    """Extra Host headers accepted beyond the loopback set (lowercased).

    ``["*"]`` means enforcement is disabled entirely. Empty by default: the
    middleware's built-in loopback set + ``HOST`` already covers local-first.
    """
    s = settings_obj or settings
    return [h.strip().lower() for h in (s.ALLOWED_HOSTS or "").split(",") if h.strip()]
