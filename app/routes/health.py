"""Health, readiness and metrics.

/health/live only says the process is running; platforms use it to decide
whether to restart the app. /health/ready also checks Redis, which every
request needs. Neither calls the geocoding provider, so health checks never
spend provider credits. /metrics requires the admin secret.
"""

from __future__ import annotations

from typing import Any

import redis.asyncio as redis
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from app import metrics
from app.auth import check_admin_secret

router = APIRouter(tags=["Health"])


@router.get("/")
async def index() -> dict[str, Any]:
    return {"service": "Geocoding API", "docs": "/docs", "health": "/health/live"}


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    checks = {"redis": "ok"}
    try:
        await request.app.state.redis.ping()
    except redis.RedisError:
        checks["redis"] = "unavailable"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ready" if ok else "unavailable", "checks": checks}, status_code=200 if ok else 503)


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> Response:
    check_admin_secret(request, request.app.state.settings.admin_secret)
    body, content_type = metrics.render()
    return Response(body, media_type=content_type)
