"""Redis-backed fault-injection controller — DEV/DEMO ONLY.

A tiny, demo-safe version of Netflix's Chaos Monkey: marks a provider as
artificially failing so the real fallback executor visibly reroutes around
it on the dashboard. Injections are always transient — the Redis key carries
a TTL so a forgotten demo resets itself, and /api/v1/admin/chaos/* is only
mounted when settings.CHAOS_MODE_ENABLED is true.

Key design (mirrors circuit_breaker.py's flat-key pattern):
    chaos:{provider}:forced_failure — literal "1", TTL = injection duration
"""

CHAOS_KEY_PREFIX = "chaos:"
CHAOS_KEY_SUFFIX = ":forced_failure"
DEFAULT_DURATION_SECONDS = 60
# Hard ceiling so no accidental misconfiguration can pin a provider down
# for longer than one demo session (the UI defaults to 60s).
MAX_DURATION_SECONDS = 3600


class ChaosController:
    def __init__(self, redis_client):
        self.redis = redis_client

    # -- key helpers --------------------------------------------------------

    @staticmethod
    def _key(provider_name: str) -> str:
        return f"{CHAOS_KEY_PREFIX}{provider_name}{CHAOS_KEY_SUFFIX}"

    @classmethod
    def _provider_from_key(cls, key: str) -> str:
        fixed = key.decode("utf-8") if isinstance(key, bytes) else key
        return fixed[len(CHAOS_KEY_PREFIX) : -len(CHAOS_KEY_SUFFIX)]

    # -- state ---------------------------------------------------------------

    async def inject_failure(
        self, provider_name: str, duration_seconds: int = DEFAULT_DURATION_SECONDS
    ) -> int:
        """Marks a provider as artificially failing.

        Sets the chaos key with TTL = duration_seconds so a forgotten test
        self-expires instead of leaving a provider permanently "broken".
        Returns the effective TTL actually applied (seconds).
        """
        ttl = max(
            1,
            min(
                int(duration_seconds or DEFAULT_DURATION_SECONDS),
                MAX_DURATION_SECONDS,
            ),
        )
        await self.redis.set(self._key(provider_name), "1", ex=ttl)
        return ttl

    async def is_chaos_active(self, provider_name: str) -> bool:
        """True while this provider has an active (unexpired) forced failure."""
        return await self.redis.get(self._key(provider_name)) is not None

    async def clear_failure(self, provider_name: str) -> None:
        """Manual reset before the TTL expires (dashboard 'Reset Now')."""
        await self.redis.delete(self._key(provider_name))

    async def get_all_active_chaos(self) -> list[str]:
        """All providers currently chaos-injected (for /models + banner)."""
        keys = await self.redis.keys(
            f"{CHAOS_KEY_PREFIX}*{CHAOS_KEY_SUFFIX}"
        )
        return sorted(self._provider_from_key(key) for key in keys)
