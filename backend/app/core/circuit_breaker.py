"""Redis-backed circuit breaker.

State machine per provider:
  CLOSED    — healthy, calls flow normally
  OPEN      — tripped, breaker skips this provider until cooldown expires
  HALF_OPEN — cooldown ended, provider is cautiously re-allowed

Redis key design (two keys, no per-provider code):
  circuit:{provider}:failures — int counter, TTL = FAILURE_WINDOW_SECONDS
      Refreshed on every failure, so the threshold counts failures within a
      rolling window that ends at the most recent failure.
  circuit:{provider}:state    — literal "OPEN", TTL = COOLDOWN_SECONDS
      When this key expires naturally and the failures counter is still >=
      threshold, get_state() reports HALF_OPEN (let a probe request through).
      A success clears both keys -> CLOSED; a failure re-arms OPEN with a
      fresh cooldown TTL.
"""

FAILURE_THRESHOLD = 5
FAILURE_WINDOW_SECONDS = 300
COOLDOWN_SECONDS = 120


class CircuitBreaker:
    def __init__(self, redis_client):
        self.redis = redis_client

    # -- key helpers --------------------------------------------------------

    @staticmethod
    def _failures_key(provider_name: str) -> str:
        return f"circuit:{provider_name}:failures"

    @staticmethod
    def _state_key(provider_name: str) -> str:
        return f"circuit:{provider_name}:state"

    @staticmethod
    def _decode(value) -> str | None:
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    @classmethod
    def _count_raw(cls, value) -> int:
        decoded = cls._decode(value)
        return int(decoded) if decoded is not None else 0

    # -- state machine ------------------------------------------------------

    async def get_state(self, provider_name: str) -> str:
        """Returns CLOSED, OPEN, or HALF_OPEN for this provider."""
        state = self._decode(await self.redis.get(self._state_key(provider_name)))
        if state == "OPEN":
            return "OPEN"

        failures = self._count_raw(
            await self.redis.get(self._failures_key(provider_name))
        )
        if failures >= FAILURE_THRESHOLD:
            return "HALF_OPEN"
        return "CLOSED"

    async def record_success(self, provider_name: str) -> None:
        """Call after a successful provider call.

        HALF_OPEN -> CLOSED (probe proved recovery; clear all state).
        CLOSED    -> decay the failure count by one (a success gradually
        heals past failures instead of ignoring them).
        """
        state = await self.get_state(provider_name)

        if state == "HALF_OPEN":
            await self.redis.delete(
                self._state_key(provider_name),
                self._failures_key(provider_name),
            )
        elif state == "CLOSED":
            failures = self._count_raw(
                await self.redis.get(self._failures_key(provider_name))
            )
            if failures > 0:
                await self.redis.decr(self._failures_key(provider_name))
        # OPEN: a success cannot normally occur through an OPEN breaker;
        # ignore to leave the cooldown untouched.

    async def record_failure(self, provider_name: str) -> None:
        """Call after a failed provider call.

        Increments the rolling failure window; trips OPEN once the count
        reaches the threshold. A failure during HALF_OPEN (the probe
        failed) immediately re-opens with a fresh cooldown TTL.
        """
        state = await self.get_state(provider_name)
        if state == "OPEN":
            # Already open — an in-flight call finishing after the trip
            # should not restart the cooldown timer.
            return

        failures_key = self._failures_key(provider_name)
        count = int(await self.redis.incr(failures_key))
        await self.redis.expire(failures_key, FAILURE_WINDOW_SECONDS)

        if state == "HALF_OPEN" or count >= FAILURE_THRESHOLD:
            await self.redis.set(
                self._state_key(provider_name),
                "OPEN",
                ex=COOLDOWN_SECONDS,
            )
