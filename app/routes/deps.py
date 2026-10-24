"""Access checks shared by the routes.

`api_access` serves the keyed API and `demo_access` serves the public
playground. Both enforce their limits in Redis before any work is done, and
both put the resulting rate-limit headers on the response.
"""

from __future__ import annotations

from dataclasses import dataclass

import redis.asyncio as redis
from fastapi import Request, Response

from app.auth import KeyRecord
from app.client_ip import client_ip
from app.errors import ApiError
from app.limits import SERVICE_UNAVAILABLE, day_window, minute_window, today_utc


@dataclass(frozen=True)
class Caller:
    kind: str  # "key", "anonymous" or "demo"
    key: KeyRecord | None = None


async def authenticate(request: Request) -> KeyRecord | None:
    """Resolve the X-API-Key header. Returns None for anonymous callers when keys are optional."""
    settings = request.app.state.settings
    supplied = request.headers.get("X-API-Key")
    if not supplied:
        if settings.require_api_key:
            raise ApiError(401, "missing_api_key", "Send your API key in the X-API-Key header.")
        return None
    try:
        record: KeyRecord | None = await request.app.state.keys.lookup(supplied)
    except redis.RedisError:
        raise SERVICE_UNAVAILABLE from None
    if record is None:
        raise ApiError(401, "invalid_api_key", "The API key is invalid or has been revoked.")
    return record


def _endpoint(request: Request) -> str:
    return request.url.path.rstrip("/").rsplit("/", 1)[-1] or "root"


async def api_access(request: Request, response: Response) -> Caller:
    settings = request.app.state.settings
    limiter = request.app.state.limiter
    key = await authenticate(request)
    if key is not None:
        decision = await limiter.check(
            [minute_window(f"key:{key.id}", key.rate_per_minute), day_window(f"key:{key.id}", key.daily_limit)],
            usage_key=f"geo:usage:{key.id}:{today_utc()}",
            usage_field=_endpoint(request),
        )
        caller = Caller("key", key)
    else:
        ip = client_ip(request, settings)
        decision = await limiter.check(
            [
                minute_window(f"anon:{ip}", settings.anon_rate_per_minute),
                day_window(f"anon:{ip}", settings.anon_daily_limit),
            ]
        )
        caller = Caller("anonymous")
    response.headers.update(decision.headers())
    return caller


async def demo_access(request: Request, response: Response) -> Caller:
    settings = request.app.state.settings
    if not settings.demo_enabled:
        raise ApiError(404, "not_found", "The demo is not enabled on this server.")
    ip = client_ip(request, settings)
    decision = await request.app.state.limiter.check(
        [
            minute_window(f"demo:{ip}", settings.demo_rate_per_minute),
            day_window(f"demo:{ip}", settings.demo_daily_per_ip),
            day_window("demo:shared:total", settings.demo_daily_total, shared=True),
        ]
    )
    response.headers.update(decision.headers())
    return Caller("demo")
