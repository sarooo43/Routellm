"""Redis-backed per-provider daily quota tracking.

Tracks request + token counts per provider per day so the router can
proactively SKIP a provider that will hit its quota, instead of burning a
real request on a guaranteed 429. Also feeds the dashboard summary.

Keys:
  quota:{provider}:{YYYY-MM-DD}:requests — int counter
  quota:{provider}:{YYYY-MM-DD}:tokens   — int counter

Both get a TTL of ~1 day so old keys self-clean (not exact-to-midnight,
which is fine for this project's scope).
"""

from datetime import datetime, timezone

# Conservative free-tier daily request limits.
# NOTE: approximate values — verify against each provider's current docs
# and update regularly (Groq and Gemini free tiers change frequently).
DAILY_REQUEST_LIMITS = {
    "groq": 14400,
    "gemini": 1500,
    "deepseek": 5000,
}

# Applied to providers with no configured limit (works for any new provider
# without code changes).
DEFAULT_DAILY_REQUEST_LIMIT = 5000

_QUOTA_TTL_SECONDS = 86400


def _today() -> str:
    # UTC so all backend instances agree on "today" regardless of host TZ.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class QuotaTracker:
    def __init__(self, redis_client):
        self.redis = redis_client

    @staticmethod
    def _requests_key(provider_name: str) -> str:
        return f"quota:{provider_name}:{_today()}:requests"

    @staticmethod
    def _tokens_key(provider_name: str) -> str:
        return f"quota:{provider_name}:{_today()}:tokens"

    @staticmethod
    def _to_int(value) -> int:
        if value is None:
            return 0
        return int(value)

    @staticmethod
    def _get_limit(provider_name: str) -> int:
        return DAILY_REQUEST_LIMITS.get(provider_name, DEFAULT_DAILY_REQUEST_LIMIT)

    async def is_within_quota(self, provider_name: str) -> bool:
        """False once today's requests reach the daily limit (hard cap:
        the request that would hit the limit is the one that gets blocked)."""
        limit = self._get_limit(provider_name)
        if limit <= 0:
            return False
        used = self._to_int(await self.redis.get(self._requests_key(provider_name)))
        return used < limit

    async def record_usage(self, provider_name: str, tokens_used: int = 0) -> None:
        """Increment today's request and token counters."""
        requests_key = self._requests_key(provider_name)
        count = await self.redis.incr(requests_key)
        if count == 1:
            # TTL anchored on the first request of the day.
            await self.redis.expire(requests_key, _QUOTA_TTL_SECONDS)

        if tokens_used:
            tokens_key = self._tokens_key(provider_name)
            if await self.redis.incrby(tokens_key, tokens_used) == tokens_used:
                # First write today — anchor the TTL here too.
                await self.redis.expire(tokens_key, _QUOTA_TTL_SECONDS)
        # tokens_used == 0: nothing to record on the token counter.

    async def get_usage_summary(self, provider_name: str) -> dict:
        """Dashboard-friendly snapshot for one provider, today."""
        requests = self._to_int(await self.redis.get(self._requests_key(provider_name)))
        tokens = self._to_int(await self.redis.get(self._tokens_key(provider_name)))
        limit = self._get_limit(provider_name)
        percentage_used = round((requests / limit) * 100, 2) if limit > 0 else 100.0
        return {
            "requests_today": requests,
            "tokens_today": tokens,
            "daily_limit": limit,
            "percentage_used": percentage_used,
        }
