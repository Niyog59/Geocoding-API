"""Geocoding routes.

`/geocode` and `/reverse` form the keyed API. `/demo/geocode` and
`/demo/reverse` serve the public playground with their own, smaller limits.
Both pairs share the same handlers.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.cache import cache_key
from app.conversion import nominatim_to_autocomplete, nominatim_to_feature
from app.errors import request_id_of
from app.limits import read_usage
from app.routes.deps import Caller, api_access, authenticate, demo_access

logger = logging.getLogger("app")
router = APIRouter()


def _validate_query(q: str) -> str:
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' cannot be empty")
    if len(q) > 500:
        raise HTTPException(status_code=400, detail="Query too long (max 500 characters)")
    return q.strip()


async def _upstream(request: Request, endpoint: str, params: dict[str, Any]) -> Any:
    state = request.app.state
    await state.provider_budget.acquire(state.settings.provider_wait_seconds)
    return await state.provider.request(endpoint, params, request_id_of(request))


async def _search(request: Request, q: str, limit: int, countrycodes: str | None, viewbox: str | None) -> dict[str, Any]:
    state = request.app.state
    start = time.time()
    q = _validate_query(q)
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
    data = await _upstream(request, "search", params)
    features = [nominatim_to_feature(item) for item in data] if data else []
    result = {
        "type": "FeatureCollection",
        "query": q.split(),
        "features": features,
        "attribution": "© OpenStreetMap contributors",
    }
    state.cache.set(ck, result)
    logger.info(f"[{request_id_of(request)}] Geocode completed: {time.time() - start:.3f}s, results={len(features)}")
    return result


async def _reverse(request: Request, lat: float, lon: float, zoom: int) -> dict[str, Any]:
    state = request.app.state
    ck = cache_key("reverse", {"lat": round(lat, 5), "lon": round(lon, 5), "zoom": zoom})
    cached = state.cache.get(ck)
    if cached is not None:
        return cached
    params = {"lat": lat, "lon": lon, "format": "json", "zoom": zoom, "addressdetails": 1}
    data = await _upstream(request, "reverse", params)
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


@router.get("/", tags=["Health"])
async def health_check(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "Geocoding API",
        "cache_entries": len(request.app.state.cache.entries),
        "api_keys_loaded": len(await request.app.state.keys.list()),
    }


@router.get("/geocode", tags=["Geocoding"])
async def geocode(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(5, ge=1, le=50),
    countrycodes: str | None = Query(None),
    viewbox: str | None = Query(None),
    caller: Caller = Depends(api_access),
) -> dict[str, Any]:
    return await _search(request, q, limit, countrycodes, viewbox)


@router.get("/reverse", tags=["Geocoding"])
async def reverse_geocode(
    request: Request,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    zoom: int = Query(18, ge=0, le=18),
    caller: Caller = Depends(api_access),
) -> dict[str, Any]:
    return await _reverse(request, lat, lon, zoom)


@router.get("/demo/geocode", tags=["Demo"])
async def demo_geocode(
    request: Request,
    q: str = Query(..., min_length=1, max_length=500),
    limit: int = Query(5, ge=1, le=50),
    countrycodes: str | None = Query(None),
    viewbox: str | None = Query(None),
    caller: Caller = Depends(demo_access),
) -> dict[str, Any]:
    return await _search(request, q, min(limit, request.app.state.settings.demo_max_results), countrycodes, viewbox)


@router.get("/demo/reverse", tags=["Demo"])
async def demo_reverse(
    request: Request,
    lat: float = Query(..., ge=-90, le=90),
    lon: float = Query(..., ge=-180, le=180),
    zoom: int = Query(18, ge=0, le=18),
    caller: Caller = Depends(demo_access),
) -> dict[str, Any]:
    return await _reverse(request, lat, lon, zoom)


@router.get("/autocomplete", tags=["Geocoding"])
async def autocomplete(
    request: Request,
    q: str = Query(..., min_length=2, max_length=200),
    limit: int = Query(5, ge=1, le=10),
    countrycodes: str | None = Query(None),
    caller: Caller = Depends(api_access),
) -> dict[str, Any]:
    state = request.app.state
    q = _validate_query(q)
    ck = cache_key("autocomplete", {"q": q.lower(), "limit": limit, "cc": countrycodes})
    cached = state.cache.get(ck)
    if cached is not None:
        return cached
    params: dict[str, Any] = {"q": q, "format": "json", "limit": limit, "addressdetails": 1, "dedupe": 1}
    if countrycodes:
        params["countrycodes"] = countrycodes.lower()
    data = await _upstream(request, "search", params)
    suggestions = [s for s in (nominatim_to_autocomplete(item) for item in (data or [])) if s]
    result = {"query": q, "suggestions": suggestions, "attribution": "© OpenStreetMap contributors"}
    state.cache.set(ck, result)
    return result


@router.get("/usage", tags=["Account"])
async def get_usage(request: Request) -> dict[str, Any]:
    """Today's usage and the last seven days for the calling key. Not counted against limits."""
    key = await authenticate(request)
    if key is None:
        raise HTTPException(status_code=401, detail="Send your API key in the X-API-Key header.")
    history = await read_usage(request.app.state.redis, key.id)
    return {
        "key": key.public(),
        "today": {"total": history[0]["total"], "endpoints": history[0]["endpoints"]},
        "last_7_days": history,
    }
