"""Rate limits, usage counters and the upstream provider budget, all kept in Redis.

Every check runs as one Lua script, so Redis evaluates it atomically: two
workers handling simultaneous requests cannot both see "one request left" and
both proceed. Windows are fixed (one per minute, one per UTC day), which keeps
the arithmetic easy to explain and lets the response headers report exactly
what the server will enforce.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import redis.asyncio as redis

from app.errors import ApiError

# KEYS: one counter per window, then (optionally) a usage hash.
# ARGV: has_usage, usage_field, usage_ttl_seconds, window_count, then limit and ttl_ms per window.
# Returns {1, count1, pttl1, count2, pttl2, ...} when allowed,
# or {0, rejected_window_index, count, pttl} when a window is already full.
RATE_LIMIT_SCRIPT = """
local n = tonumber(ARGV[4])
for i = 1, n do
  local limit = tonumber(ARGV[3 + 2 * i])
  local current = tonumber(redis.call('GET', KEYS[i]) or '0')
  if current >= limit then
    return {0, i, current, redis.call('PTTL', KEYS[i])}
  end
end
local out = {1}
for i = 1, n do
  local count = redis.call('INCR', KEYS[i])
  if count == 1 then
    redis.call('PEXPIRE', KEYS[i], tonumber(ARGV[4 + 2 * i]))
  end
  table.insert(out, count)
  table.insert(out, redis.call('PTTL', KEYS[i]))
end
if ARGV[1] == '1' then
  local usage = KEYS[n + 1]
  redis.call('HINCRBY', usage, 'total', 1)
  redis.call('HINCRBY', usage, ARGV[2], 1)
  redis.call('EXPIRE', usage, tonumber(ARGV[3]))
end
return out
"""

# KEYS: daily budget counter, per-second counter.
# ARGV: daily_limit, day_ttl_ms, per_second_limit, second_ttl_ms.
# Returns {1, used_today} when a request may go upstream, {0, pttl} when the
# daily budget is spent, or {2, pttl} when this second's allowance is used up.
PROVIDER_BUDGET_SCRIPT = """
local today = tonumber(redis.call('GET', KEYS[1]) or '0')
if today >= tonumber(ARGV[1]) then
  return {0, redis.call('PTTL', KEYS[1])}
end
local second = tonumber(redis.call('GET', KEYS[2]) or '0')
if second >= tonumber(ARGV[3]) then
  return {2, redis.call('PTTL', KEYS[2])}
end
local used = redis.call('INCR', KEYS[1])
if used == 1 then redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2])) end
local s = redis.call('INCR', KEYS[2])
if s == 1 then redis.call('PEXPIRE', KEYS[2], tonumber(ARGV[4])) end
return {1, used}
"""

SERVICE_UNAVAILABLE = ApiError(503, "service_unavailable", "The service is temporarily unavailable.")


def _utc_now() -> datetime:
    return datetime.fromtimestamp(time.time(), UTC)


def ms_until_utc_midnight(now: datetime | None = None) -> int:
    now = now or _utc_now()
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((midnight - now).total_seconds() * 1000))


def today_utc() -> str:
    return _utc_now().strftime("%Y%m%d")


@dataclass(frozen=True)
class Window:
    """One counter: at most `limit` requests until the window key expires."""

    label: str  # "minute" or "day"
    key: str
    limit: int
    ttl_ms: int
    # A shared window caps everyone together (for example the demo's daily
    # total). Hitting it means the service quota is spent, not that this caller
    # was too fast, so it is reported as quota_exhausted rather than rate_limited.
    shared: bool = False


@dataclass(frozen=True)
class Decision:
    limit: int
    remaining: int
    reset_seconds: int

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_seconds),
        }


RATE_LIMITED = (429, "rate_limited", "Too many requests. Please wait before trying again.")
QUOTA_EXHAUSTED = (
    503,
    "quota_exhausted",
    "The usage limit for this service has been reached. Please try again after it resets.",
)


def minute_window(scope: str, limit: int) -> Window:
    now = time.time()
    index = int(now // 60)
    ttl_ms = max(1, math.ceil((60 - now % 60) * 1000))
    return Window("minute", f"geo:rl:{scope}:m:{index}", limit, ttl_ms)


def day_window(scope: str, limit: int, shared: bool = False) -> Window:
    return Window("day", f"geo:rl:{scope}:d:{today_utc()}", limit, ms_until_utc_midnight(), shared)


class RateLimiter:
    def __init__(self, client: redis.Redis, usage_retention_days: int) -> None:
        self.script = client.register_script(RATE_LIMIT_SCRIPT)
        self.usage_ttl = usage_retention_days * 86400

    async def check(
        self,
        windows: list[Window],
        usage_key: str | None = None,
        usage_field: str = "",
    ) -> Decision:
        keys = [w.key for w in windows] + ([usage_key] if usage_key else [])
        args: list[str | int] = ["1" if usage_key else "0", usage_field, self.usage_ttl, len(windows)]
        for w in windows:
            args += [w.limit, w.ttl_ms]
        try:
            result = await self.script(keys=keys, args=args)
        except redis.RedisError:
            raise SERVICE_UNAVAILABLE from None
        if result[0] == 0:
            window = windows[int(result[1]) - 1]
            retry = max(1, math.ceil(int(result[3]) / 1000))
            decision = Decision(window.limit, 0, retry)
            status, code, message = QUOTA_EXHAUSTED if window.shared else RATE_LIMITED
            raise ApiError(status, code, message, {**decision.headers(), "Retry-After": str(retry)})
        best: Decision | None = None
        for i, w in enumerate(windows):
            count, pttl = int(result[1 + 2 * i]), int(result[2 + 2 * i])
            candidate = Decision(w.limit, max(0, w.limit - count), max(1, math.ceil(pttl / 1000)))
            if best is None or (candidate.remaining, candidate.reset_seconds) < (best.remaining, best.reset_seconds):
                best = candidate
        assert best is not None
        return best


class ProviderBudget:
    """A daily request allowance and a per-second throttle for the upstream provider.

    Shared through Redis, so every worker and replica draws from the same
    budget, and every upstream attempt (including retries) is counted.
    """

    def __init__(self, client: redis.Redis, provider: str, daily_limit: int, per_second: int) -> None:
        self.script = client.register_script(PROVIDER_BUDGET_SCRIPT)
        self.provider = provider
        self.daily_limit = daily_limit
        self.per_second = per_second

    async def acquire(self, wait_seconds: float) -> None:
        give_up_at = time.monotonic() + wait_seconds
        while True:
            now = time.time()
            keys = [
                f"geo:provider:{self.provider}:d:{today_utc()}",
                f"geo:provider:{self.provider}:s:{int(now)}",
            ]
            args = [self.daily_limit, ms_until_utc_midnight(), self.per_second, 2000]
            try:
                status, pttl = await self.script(keys=keys, args=args)
            except redis.RedisError:
                raise SERVICE_UNAVAILABLE from None
            if status == 1:
                return
            if status == 0:
                retry = max(1, math.ceil(int(pttl) / 1000))
                raise ApiError(
                    503,
                    "quota_exhausted",
                    "The daily usage limit for the geocoding provider has been reached.",
                    {"Retry-After": str(retry)},
                )
            pause = max(0.02, 1 - (now % 1))
            if time.monotonic() + pause > give_up_at:
                raise ApiError(
                    503, "upstream_busy", "The service is busy. Please try again shortly.", {"Retry-After": "1"}
                )
            await asyncio.sleep(pause)


async def read_usage(client: redis.Redis, key_id: str, days: int = 7) -> list[dict[str, object]]:
    now = _utc_now()
    dates = [(now - timedelta(days=offset)).strftime("%Y%m%d") for offset in range(days)]
    try:
        async with client.pipeline(transaction=False) as pipe:
            for date in dates:
                pipe.hgetall(f"geo:usage:{key_id}:{date}")
            rows = await pipe.execute()
    except redis.RedisError:
        raise SERVICE_UNAVAILABLE from None
    history = []
    for date, row in zip(dates, rows, strict=True):
        endpoints = {k: int(v) for k, v in row.items() if k != "total"}
        history.append(
            {"date": f"{date[:4]}-{date[4:6]}-{date[6:]}", "total": int(row.get("total", 0)), "endpoints": endpoints}
        )
    return history
