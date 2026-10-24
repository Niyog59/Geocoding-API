"""Request dependencies shared by the routes."""

from __future__ import annotations

import redis.asyncio as redis
from fastapi import Request

from app.auth import KeyRecord
from app.errors import ApiError


async def authenticate(request: Request) -> KeyRecord | None:
    """Resolve the X-API-Key header. Returns None for anonymous callers when keys are optional."""
    settings = request.app.state.settings
    supplied = request.headers.get("X-API-Key")
    if not supplied:
        if settings.require_api_key:
            raise ApiError(401, "missing_api_key", "Send your API key in the X-API-Key header.")
        return None
    try:
        record = await request.app.state.keys.lookup(supplied)
    except redis.RedisError:
        raise ApiError(503, "service_unavailable", "The service is temporarily unavailable.") from None
    if record is None:
        raise ApiError(401, "invalid_api_key", "The API key is invalid or has been revoked.")
    request.state.key = record
    return record
