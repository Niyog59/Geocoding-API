"""HTTP routes."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from slowapi import Limiter

from app.cache import MemoryCache, cache_key
from app.config import Settings
from app.conversion import nominatim_to_autocomplete, nominatim_to_feature
from app.keys import KeyStore
from app.providers.nominatim import NominatimClient

logger = logging.getLogger("app")


def _validate_query(q: str) -> str:
    if not q or not q.strip():
        raise HTTPException(status_code=400, detail="Query parameter 'q' cannot be empty")
    if len(q) > 500:
        raise HTTPException(status_code=400, detail="Query too long (max 500 characters)")
    return q.strip()


def _validate_coordinates(lat: float, lon: float) -> tuple[float, float]:
    if not (-90 <= lat <= 90):
        raise HTTPException(status_code=400, detail="Latitude must be between -90 and 90")
    if not (-180 <= lon <= 180):
        raise HTTPException(status_code=400, detail="Longitude must be between -180 and 180")
    return lat, lon


def build_router(settings: Settings, limiter: Limiter, keys: KeyStore, cache: MemoryCache, provider: NominatimClient) -> APIRouter:
    router = APIRouter()

    async def get_api_key(
        request: Request, x_api_key: str | None = Header(None), api_key: str | None = Query(None)
    ) -> str | None:
        key = x_api_key or api_key
        if settings.require_api_key and not key:
            raise HTTPException(
                status_code=401, detail="API key required. Include X-API-Key header or api_key query parameter."
            )
        if key:
            key_data = keys.validate(key)
            if not key_data:
                raise HTTPException(status_code=401, detail="Invalid API key")
            request.state.api_key_data = key_data
        return key

    def request_id_of(request: Request) -> str:
        return getattr(request.state, "request_id", "unknown")

    @router.get("/", tags=["Health"])
    async def health_check() -> dict[str, Any]:
        return {
            "status": "ok",
            "service": "Nominatim Geocoding API",
            "version": "2.0.0",
            "cache_entries": len(cache.entries),
            "api_keys_loaded": len(keys.keys),
        }

    @router.get("/geocode", tags=["Geocoding"])
    @limiter.limit(settings.rate_limit)
    async def geocode(
        request: Request,
        q: str = Query(..., min_length=1, max_length=500),
        limit: int = Query(5, ge=1, le=50),
        countrycodes: str | None = Query(None),
        viewbox: str | None = Query(None),
        api_key: str | None = Depends(get_api_key),
    ) -> dict[str, Any]:
        request_id = request_id_of(request)
        start = time.time()
        q = _validate_query(q)
        keys.track_usage(api_key, "geocode")
        logger.info(f"[{request_id}] Geocode: q='{q[:50]}', limit={limit}")
        key = cache_key("geocode", {"q": q.lower(), "limit": limit, "countrycodes": countrycodes})
        cached = cache.get(key)
        if cached is not None:
            return cached
        params: dict[str, Any] = {"q": q, "format": "json", "limit": limit, "addressdetails": 1}
        if countrycodes:
            params["countrycodes"] = countrycodes.lower()
        if viewbox:
            params["viewbox"] = viewbox
            params["bounded"] = 1
        data = await provider.request("search", params, request_id)
        features = [nominatim_to_feature(item) for item in data] if data else []
        result = {
            "type": "FeatureCollection",
            "query": q.split(),
            "features": features,
            "attribution": "© OpenStreetMap contributors",
        }
        cache.set(key, result)
        logger.info(f"[{request_id}] Geocode completed: {time.time() - start:.3f}s, results={len(features)}")
        return result

    @router.get("/reverse", tags=["Geocoding"])
    @limiter.limit(settings.rate_limit)
    async def reverse_geocode(
        request: Request,
        lat: float = Query(..., ge=-90, le=90),
        lon: float = Query(..., ge=-180, le=180),
        zoom: int = Query(18, ge=0, le=18),
        api_key: str | None = Depends(get_api_key),
    ) -> dict[str, Any]:
        request_id = request_id_of(request)
        lat, lon = _validate_coordinates(lat, lon)
        keys.track_usage(api_key, "reverse")
        key = cache_key("reverse", {"lat": round(lat, 5), "lon": round(lon, 5), "zoom": zoom})
        cached = cache.get(key)
        if cached is not None:
            return cached
        params = {"lat": lat, "lon": lon, "format": "json", "zoom": zoom, "addressdetails": 1}
        data = await provider.request("reverse", params, request_id)
        if isinstance(data, dict) and "error" in data:
            raise HTTPException(status_code=404, detail=data["error"])
        result = {
            "type": "FeatureCollection",
            "query": [str(lon), str(lat)],
            "features": [nominatim_to_feature(data, is_reverse=True)],
            "attribution": "© OpenStreetMap contributors",
        }
        cache.set(key, result)
        return result

    @router.get("/autocomplete", tags=["Geocoding"])
    @limiter.limit(settings.rate_limit_authenticated)
    async def autocomplete(
        request: Request,
        q: str = Query(..., min_length=2, max_length=200),
        limit: int = Query(5, ge=1, le=10),
        countrycodes: str | None = Query(None),
        api_key: str | None = Depends(get_api_key),
    ) -> dict[str, Any]:
        request_id = request_id_of(request)
        q = _validate_query(q)
        keys.track_usage(api_key, "autocomplete")
        key = cache_key("autocomplete", {"q": q.lower(), "limit": limit, "cc": countrycodes})
        cached = cache.get(key)
        if cached is not None:
            return cached
        params: dict[str, Any] = {"q": q, "format": "json", "limit": limit, "addressdetails": 1, "dedupe": 1}
        if countrycodes:
            params["countrycodes"] = countrycodes.lower()
        data = await provider.request("search", params, request_id)
        suggestions = [s for s in (nominatim_to_autocomplete(item) for item in (data or [])) if s]
        result = {"query": q, "suggestions": suggestions, "attribution": "© OpenStreetMap contributors"}
        cache.set(key, result)
        return result

    @router.get("/usage", tags=["Account"])
    async def get_usage_stats(request: Request, api_key: str = Depends(get_api_key)) -> dict[str, Any]:
        if not api_key:
            raise HTTPException(status_code=401, detail="API key required")
        key_data = keys.validate(api_key)
        return {
            "api_key": f"{api_key[:10]}...{api_key[-4:]}",
            "name": key_data.get("name", "Unknown") if key_data else "Unknown",
            "rate_limit": key_data.get("rate_limit", settings.rate_limit) if key_data else settings.rate_limit,
            "usage": keys.get_usage(api_key),
        }

    @router.post("/keys/generate", tags=["Admin"])
    async def generate_api_key(
        request: Request,
        name: str = Query(..., min_length=1, max_length=100),
        master_key: str = Query(...),
    ) -> dict[str, Any]:
        if not settings.master_api_key or master_key != settings.master_api_key:
            raise HTTPException(status_code=403, detail="Invalid master key")
        new_key = keys.generate(name, settings.rate_limit_authenticated)
        logger.info(f"New API key generated: {name}")
        return {
            "api_key": new_key,
            "name": name,
            "rate_limit": settings.rate_limit_authenticated,
            "message": "Store this key securely - it cannot be retrieved later!",
        }

    return router
