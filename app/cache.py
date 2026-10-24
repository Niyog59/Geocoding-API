"""Shared response cache in Redis.

The cache key is a hash of every input that can change the answer: the
operation, provider, normalised query or coordinates, result limit, country
filter, viewbox and language. Leaving one out (as the original code did with
`viewbox`) lets one request receive another request's results.

Storage is bounded two ways: every entry expires, and an index of entries is
trimmed to `max_entries`, oldest first. Empty results are cached for a shorter
time than non-empty ones. Errors are never cached.

When several requests miss the same key at once, only one of them calls the
provider. Within a worker the others await the same task; across workers they
wait for a short Redis lock to be released and then read the stored result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

import redis.asyncio as redis

from app.limits import SERVICE_UNAVAILABLE

CacheStatus = Literal["HIT", "MISS", "SHARED"]

# All cache keys share the {geocache} hash tag. The store script deletes the
# oldest entries, whose names it only learns from the index; hosted Redis
# services (Upstash) allow a script to touch such keys only when they share a
# hash tag with a declared key.
PREFIX = "{geocache}:"
INDEX_KEY = PREFIX + "index"

# KEYS: entry key, index key. ARGV: value, ttl_ms, now_ms, max_entries.
# The index holds one member per stored entry, ordered by store time. It is
# trimmed by size only; members of entries that already expired are removed
# when they become the oldest.
STORE_SCRIPT = """
redis.call('SET', KEYS[1], ARGV[1], 'PX', tonumber(ARGV[2]))
redis.call('ZADD', KEYS[2], tonumber(ARGV[3]), KEYS[1])
local extra = redis.call('ZCARD', KEYS[2]) - tonumber(ARGV[4])
if extra > 0 then
  local oldest = redis.call('ZPOPMIN', KEYS[2], extra)
  for i = 1, #oldest, 2 do
    redis.call('DEL', oldest[i])
  end
end
return 1
"""

# KEYS: lock key. ARGV: token. Deletes the lock only if this request still holds it.
RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def cache_key(identity: dict[str, Any]) -> str:
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return PREFIX + "entry:" + hashlib.sha256(canonical.encode()).hexdigest()


class ResponseCache:
    def __init__(
        self,
        client: redis.Redis,
        ttl_seconds: int,
        empty_ttl_seconds: int,
        max_entries: int,
        max_entry_bytes: int,
        lock_seconds: float,
        wait_seconds: float,
    ) -> None:
        self.redis = client
        self.ttl_ms = ttl_seconds * 1000
        self.empty_ttl_ms = empty_ttl_seconds * 1000
        self.max_entries = max_entries
        self.max_entry_bytes = max_entry_bytes
        self.lock_ms = int(lock_seconds * 1000)
        self.wait_seconds = wait_seconds
        self.store_script = client.register_script(STORE_SCRIPT)
        self.release_script = client.register_script(RELEASE_SCRIPT)
        self._inflight: dict[str, asyncio.Task[tuple[list[dict[str, Any]], bool]]] = {}

    async def get(self, key: str) -> list[dict[str, Any]] | None:
        try:
            raw = await self.redis.get(key)
        except redis.RedisError:
            raise SERVICE_UNAVAILABLE from None
        return None if raw is None else json.loads(raw)["features"]

    async def put(self, key: str, features: list[dict[str, Any]]) -> bool:
        """Store a result. Returns False when it is too large to cache."""
        value = json.dumps({"features": features, "stored_at": int(time.time())}, separators=(",", ":"))
        if len(value.encode()) > self.max_entry_bytes:
            return False
        ttl = self.ttl_ms if features else self.empty_ttl_ms
        await self.store_script(keys=[key, INDEX_KEY], args=[value, ttl, int(time.time() * 1000), self.max_entries])
        return True

    async def get_or_fetch(
        self, key: str, fetch: Callable[[], Awaitable[list[dict[str, Any]]]]
    ) -> tuple[list[dict[str, Any]], CacheStatus]:
        cached = await self.get(key)
        if cached is not None:
            return cached, "HIT"
        # Same worker: share one task.
        task = self._inflight.get(key)
        if task is not None:
            features, _ = await asyncio.shield(task)
            return features, "SHARED"
        task = asyncio.ensure_future(self._fetch_once(key, fetch))
        self._inflight[key] = task
        task.add_done_callback(lambda _: self._inflight.pop(key, None))
        features, waited = await asyncio.shield(task)
        return features, "SHARED" if waited else "MISS"

    async def _fetch_once(
        self, key: str, fetch: Callable[[], Awaitable[list[dict[str, Any]]]]
    ) -> tuple[list[dict[str, Any]], bool]:
        lock_key = key.replace(PREFIX + "entry:", PREFIX + "lock:", 1)
        token = secrets.token_hex(8)
        try:
            locked = await self.redis.set(lock_key, token, nx=True, px=self.lock_ms)
        except redis.RedisError:
            raise SERVICE_UNAVAILABLE from None
        if not locked:
            # Another worker is fetching. Wait for its result instead of calling the provider again.
            deadline = time.monotonic() + self.wait_seconds
            polls = 0
            while time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                polls += 1
                cached = await self.get(key)
                if cached is not None:
                    return cached, True
                if polls % 5 == 0 and not await self.redis.exists(lock_key):
                    break  # the other request failed; fetch ourselves
            locked = await self.redis.set(lock_key, token, nx=True, px=self.lock_ms)
        # After a stored success the lock is left to expire: waiters find the
        # stored result, and skipping the release saves Redis calls. On failure,
        # or when the result was too large to store, the lock is released so
        # waiting requests stop waiting.
        try:
            features = await fetch()
            stored = await self.put(key, features)
        except BaseException:
            await self._release(locked, lock_key, token)
            raise
        if not stored:
            await self._release(locked, lock_key, token)
        return features, False

    async def _release(self, locked: bool | None, lock_key: str, token: str) -> None:
        if not locked:
            return
        try:
            await self.release_script(keys=[lock_key], args=[token])
        except redis.RedisError:
            pass  # the lock expires on its own
