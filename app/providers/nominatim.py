"""Adapter for a Nominatim server.

Use only an instance you are allowed to use for this purpose, such as your own.
The public nominatim.openstreetmap.org service forbids autocomplete and heavy
or redistributed use, so it is not a default.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import HTTPException

from app.places import Place, make_label
from app.validation import ReverseParams, SearchParams

logger = logging.getLogger("app")

ATTRIBUTION = [{"text": "© OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"}]


def _place(item: dict[str, Any]) -> Place:
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=502, detail="Invalid response from geocoding service") from None
    raw = item.get("address") or {}
    address = {
        "housenumber": raw.get("house_number"),
        "street": raw.get("road"),
        "suburb": raw.get("suburb"),
        "city": raw.get("city") or raw.get("town") or raw.get("village"),
        "county": raw.get("county"),
        "state": raw.get("state"),
        "postcode": raw.get("postcode"),
        "country": raw.get("country"),
        "country_code": raw.get("country_code"),
    }
    address = {k: str(v) for k, v in address.items() if v}
    name = item.get("name") or None
    place_id = item.get("place_id")
    return Place(
        lat=lat,
        lon=lon,
        label=make_label(item.get("display_name"), name, address, lat, lon),
        provider="nominatim",
        name=name,
        kind=item.get("addresstype") or item.get("type"),
        address=address,
        provider_place_id=str(place_id) if place_id is not None else None,
    )


class NominatimProvider:
    name = "nominatim"
    attribution = ATTRIBUTION

    def __init__(self, base_url: str, user_agent: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent

    async def _get(self, endpoint: str, params: dict[str, Any]) -> Any:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        try:
            async with httpx.AsyncClient(headers=headers, timeout=15.0) as client:
                response = await client.get(f"{self.base_url}/{endpoint}", params=params)
                if response.status_code == 403:
                    raise HTTPException(status_code=503, detail="Geocoding service temporarily unavailable")
                if response.status_code == 429:
                    raise HTTPException(status_code=429, detail="Geocoding service rate limit exceeded")
                response.raise_for_status()
                return response.json()
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Geocoding service timeout") from None
        except httpx.HTTPStatusError:
            raise HTTPException(status_code=502, detail="Geocoding service error") from None
        except httpx.RequestError:
            raise HTTPException(status_code=502, detail="Failed to connect to geocoding service") from None

    async def search(self, p: SearchParams) -> list[Place]:
        params: dict[str, Any] = {"q": p.q, "format": "jsonv2", "limit": p.limit, "addressdetails": 1}
        if p.countrycodes:
            params["countrycodes"] = ",".join(p.countrycodes)
        if p.viewbox:
            params["viewbox"] = ",".join(f"{v:.6f}" for v in p.viewbox)
            params["bounded"] = 1
        if p.lang:
            params["accept-language"] = p.lang
        data = await self._get("search", params)
        return [_place(item) for item in data or []]

    async def reverse(self, p: ReverseParams) -> list[Place]:
        params: dict[str, Any] = {"lat": f"{p.lat:.6f}", "lon": f"{p.lon:.6f}", "format": "jsonv2", "addressdetails": 1}
        if p.lang:
            params["accept-language"] = p.lang
        data = await self._get("reverse", params)
        if isinstance(data, dict) and "error" in data:
            return []  # Nominatim reports "no address here" this way.
        return [_place(data)]
