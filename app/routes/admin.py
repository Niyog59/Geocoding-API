"""Administration routes. They require the X-Admin-Secret header, never an API key."""

from __future__ import annotations

from typing import Any

import redis.asyncio as redis
from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, Field

from app.auth import check_admin_secret
from app.errors import ApiError

router = APIRouter(prefix="/admin", tags=["Admin"])


class NewKey(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    rate_per_minute: int | None = Field(default=None, ge=1)
    daily_limit: int | None = Field(default=None, ge=1)


def _require_admin(request: Request) -> None:
    check_admin_secret(request, request.app.state.settings.admin_secret)


@router.post("/keys", status_code=201)
async def create_key(request: Request, body: NewKey) -> dict[str, Any]:
    """Create an API key. The key is returned once and cannot be retrieved later."""
    _require_admin(request)
    settings = request.app.state.settings
    rate = body.rate_per_minute or settings.default_key_rate_per_minute
    daily = body.daily_limit or settings.default_key_daily_limit
    if rate > settings.max_key_rate_per_minute:
        raise ApiError(400, "invalid_request", f"rate_per_minute must be at most {settings.max_key_rate_per_minute}.")
    if daily > settings.max_key_daily_limit:
        raise ApiError(400, "invalid_request", f"daily_limit must be at most {settings.max_key_daily_limit}.")
    try:
        api_key, record = await request.app.state.keys.create(body.name, rate, daily)
    except redis.RedisError:
        raise ApiError(503, "service_unavailable", "The service is temporarily unavailable.") from None
    return {**record.public(), "api_key": api_key, "note": "Store this key now. It is shown only once."}


@router.get("/keys")
async def list_keys(request: Request) -> dict[str, Any]:
    _require_admin(request)
    return {"keys": [record.public() for record in await request.app.state.keys.list()]}


@router.delete("/keys/{key_id}", status_code=204)
async def revoke_key(request: Request, key_id: str) -> Response:
    _require_admin(request)
    if not await request.app.state.keys.revoke(key_id):
        raise ApiError(404, "not_found", "No key with that id.")
    return Response(status_code=204)
