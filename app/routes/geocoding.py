"""Geocoding routes."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from slowapi import Limiter

from app.auth import KeyRecord
from app.cache import cache_key
from app.config import Settings
from app.conversion import nominatim_to_autocomplete, nominatim_to_feature
from app.errors import request_id_of
from app.routes.deps import authenticate

logger = logging.getLogger("app")


def _validate_query(q: str) -> str:
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' cannot be empty")
    if len(q) > 500:
        raise HTTPException(status_code=400, detail="Query too long (max 500 characters)")
    return q.strip()


def build_router(settings: Settings, limiter: Limiter) -> APIRouter:
    router = APIRouter()

    @router.get("/", tags=["Health"])
    async def health_check(request: Request) -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "Geocoding API",
            "cache_entries": len(request.app.state.cache.entries),
            "api_keys_loaded": len(await request.app.state.keys.list()),
        }

    @router.get("/geocode", tags=["Geocoding"])
    @limiter.limit(settings.rate_limit)
    async def geocode(
        request: Request,
        q: str = Query(..., min_length=1, max_length=500),
        limit: int = Query(5, ge=1, le=50),
        countrycodes: str | None = Query(None),
        viewbox: str | None = Query(None),
        key: KeyRecord | None = Depends(authenticate),
    ) -> dict[str, Any]:
        state = request.app.state
        request_id = request_id_of(request)
        start = time.time()
        q = _validate_query(q)
        state.usage.track(key.id if key else None, "geocode")
        logger.info(f"[{request_id}] Geocode: q='{q[:50]}', limit={limit}")
        ck = cache_key("geocode", {"q": q.lower(), "limit": limit, "countrycodes": countrycodes})
        cached = state.cache.get(ck)
        if cached is not None:
            return cached
        params: dict[str, Any] = {"q": q, "format": "json", "limit": limit, "addressdetails": 1}
        if countrycodes:
            params["countrycodes"] = countrycodes.lower()
        if viewbox:
            params["viewbox"] = viewbox
            params["bounded"] = 1
        data = await state.provider.request("search", params, request_id)
        features = [nominatim_to_feature(item) for item in data] if data else []
        result = {
            "type": "FeatureCollection",
            "query": q.split(),
            "features": features,
            "attribution": "© OpenStreetMap contributors",
        }
        state.cache.set(ck, result)
        logger.info(f"[{request_id}] Geocode completed: {time.time() - start:.3f}s, results={len(features)}")
        return result

    @router.get("/reverse", tags=["Geocoding"])
    @limiter.limit(settings.rate_limit)
    async def reverse_geocode(
        request: Request,
        lat: float = Query(..., ge=-90, le=90),
        lon: float = Query(..., ge=-180, le=180),
        zoom: int = Query(18, ge=0, le=18),
        key: KeyRecord | None = Depends(authenticate),
    ) -> dict[str, Any]:
        state = request.app.state
        state.usage.track(key.id if key else None, "reverse")
        ck = cache_key("reverse", {"lat": round(lat, 5), "lon": round(lon, 5), "zoom": zoom})
        cached = state.cache.get(ck)
        if cached is not None:
            return cached
        params = {"lat": lat, "lon": lon, "format": "json", "zoom": zoom, "addressdetails": 1}
        data = await state.provider.request("reverse", params, request_id_of(request))
        if isinstance(data, dict) and "error" in data:
            raise HTTPException(status_code=404, detail=data["error"])
        result = {
            "type": "FeatureCollection",
            "query": [str(lon), str(lat)],
            "features": [nominatim_to_feature(data, is_reverse=True)],
            "attribution": "© OpenStreetMap contributors",
        }
        state.cache.set(ck, result)
        return result

    @router.get("/autocomplete", tags=["Geocoding"])
    @limiter.limit(settings.rate_limit)
    async def autocomplete(
        request: Request,
        q: str = Query(..., min_length=2, max_length=200),
        limit: int = Query(5, ge=1, le=10),
        countrycodes: str | None = Query(None),
        key: KeyRecord | None = Depends(authenticate),
    ) -> dict[str, Any]:
        state = request.app.state
        q = _validate_query(q)
        state.usage.track(key.id if key else None, "autocomplete")
        ck = cache_key("autocomplete", {"q": q.lower(), "limit": limit, "cc": countrycodes})
        cached = state.cache.get(ck)
        if cached is not None:
            return cached
        params: dict[str, Any] = {"q": q, "format": "json", "limit": limit, "addressdetails": 1, "dedupe": 1}
        if countrycodes:
            params["countrycodes"] = countrycodes.lower()
        data = await state.provider.request("search", params, request_id_of(request))
        suggestions = [s for s in (nominatim_to_autocomplete(item) for item in (data or [])) if s]
        result = {"query": q, "suggestions": suggestions, "attribution": "© OpenStreetMap contributors"}
        state.cache.set(ck, result)
        return result

    @router.get("/usage", tags=["Account"])
    async def get_usage_stats(request: Request, key: KeyRecord | None = Depends(authenticate)) -> dict[str, Any]:
        if key is None:
            raise HTTPException(status_code=401, detail="API key required")
        return {"key": key.public(), "usage": request.app.state.usage.get(key.id)}

    return router
