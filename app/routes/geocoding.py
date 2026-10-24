"""Geocoding routes.

`/geocode` and `/reverse` form the keyed API. `/demo/geocode` and
`/demo/reverse` serve the public playground with their own, smaller limits.
Both pairs share the same handlers.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response

from app.errors import ApiError
from app.limits import read_usage
from app.routes.deps import Caller, api_access, authenticate, demo_access
from app.validation import reverse_params, search_params

router = APIRouter()

SEARCH_DOC = {
    "parameters": [
        {"name": "q", "in": "query", "required": True, "schema": {"type": "string", "maxLength": 200}},
        {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 10, "default": 5}},
        {
            "name": "countrycodes",
            "in": "query",
            "schema": {"type": "string"},
            "description": "Comma-separated ISO 3166-1 alpha-2 codes",
        },
        {
            "name": "viewbox",
            "in": "query",
            "schema": {"type": "string"},
            "description": "min_lon,min_lat,max_lon,max_lat",
        },
        {"name": "lang", "in": "query", "schema": {"type": "string"}, "description": "ISO 639-1 language code"},
    ]
}
REVERSE_DOC = {
    "parameters": [
        {"name": "lat", "in": "query", "required": True, "schema": {"type": "number", "minimum": -90, "maximum": 90}},
        {"name": "lon", "in": "query", "required": True, "schema": {"type": "number", "minimum": -180, "maximum": 180}},
        {"name": "lang", "in": "query", "schema": {"type": "string"}},
    ]
}


async def _search(request: Request, response: Response, max_limit: int) -> dict[str, Any]:
    params = search_params(request, max_limit)
    body: dict[str, Any]
    body, status = await request.app.state.service.search(params)
    response.headers["X-Cache"] = status
    return body


async def _reverse(request: Request, response: Response) -> dict[str, Any]:
    params = reverse_params(request)
    body: dict[str, Any]
    body, status = await request.app.state.service.reverse(params)
    response.headers["X-Cache"] = status
    return body


@router.get("/geocode", tags=["Geocoding"], openapi_extra=SEARCH_DOC)
async def geocode(request: Request, response: Response, caller: Caller = Depends(api_access)) -> dict[str, Any]:
    """Find places matching an address or place name. Requires an API key."""
    return await _search(request, response, 10)


@router.get("/reverse", tags=["Geocoding"], openapi_extra=REVERSE_DOC)
async def reverse(request: Request, response: Response, caller: Caller = Depends(api_access)) -> dict[str, Any]:
    """Find the address closest to a coordinate. Requires an API key."""
    return await _reverse(request, response)


@router.get("/demo/geocode", tags=["Demo"], openapi_extra=SEARCH_DOC)
async def demo_geocode(request: Request, response: Response, caller: Caller = Depends(demo_access)) -> dict[str, Any]:
    """Search for the public playground, with small per-visitor and daily limits."""
    return await _search(request, response, request.app.state.settings.demo_max_results)


@router.get("/demo/reverse", tags=["Demo"], openapi_extra=REVERSE_DOC)
async def demo_reverse(request: Request, response: Response, caller: Caller = Depends(demo_access)) -> dict[str, Any]:
    """Reverse lookup for the public playground, with small per-visitor and daily limits."""
    return await _reverse(request, response)


@router.get("/autocomplete", tags=["Geocoding"])
async def autocomplete(request: Request, response: Response, caller: Caller = Depends(api_access)) -> dict[str, Any]:
    body = await _search(request, response, 10)
    suggestions = [
        {"id": f["id"], "text": f["properties"]["label"], "center": f["geometry"]["coordinates"]}
        for f in body["features"]
    ]
    return {"query": body["query"]["q"], "suggestions": suggestions, "attribution": body["attribution"]}


@router.get("/usage", tags=["Account"])
async def get_usage(request: Request) -> dict[str, Any]:
    """Today's usage and the last seven days for the calling key. Not counted against limits."""
    key = await authenticate(request)
    if key is None:
        raise ApiError(401, "missing_api_key", "Send your API key in the X-API-Key header.")
    history = await read_usage(request.app.state.redis, key.id)
    return {
        "key": key.public(),
        "today": {"total": history[0]["total"], "endpoints": history[0]["endpoints"]},
        "last_7_days": history,
    }
