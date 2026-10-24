"""Adapter for the Geoapify Geocoding API.

Geoapify's terms require "Powered by Geoapify" attribution on the Free plan and
OpenStreetMap attribution on every plan; both are returned with each response.
The provider key stays on the server and is sent only to Geoapify.

Parameter mapping:

    q            -> text
    limit        -> limit
    countrycodes -> filter=countrycode:xx,yy
    viewbox      -> filter=rect:min_lon,min_lat,max_lon,max_lat
    lang         -> lang

Geoapify combines filters with "|" (all must match), which gives the same
meaning as Nominatim's bounded viewbox and country filter.
"""

from __future__ import annotations

from typing import Any

from app.errors import ApiError
from app.places import Place, make_label
from app.upstream import UpstreamClient
from app.validation import ReverseParams, SearchParams

ATTRIBUTION = [
    {"text": "Powered by Geoapify", "url": "https://www.geoapify.com/"},
    {"text": "© OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"},
]
BAD_RESPONSE = ApiError(502, "upstream_bad_response", "The geocoding provider sent an unreadable response.")
ADDRESS_FIELDS = ("housenumber", "street", "suburb", "city", "county", "state", "postcode", "country", "country_code")


def _place(item: Any) -> Place:
    if not isinstance(item, dict):
        raise BAD_RESPONSE
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (KeyError, ValueError, TypeError):
        raise BAD_RESPONSE from None
    address = {k: str(item[k]) for k in ADDRESS_FIELDS if item.get(k)}
    name = item.get("name") or None
    rank = item.get("rank")
    confidence = rank.get("confidence") if isinstance(rank, dict) else None
    place_id = item.get("place_id")
    return Place(
        lat=lat,
        lon=lon,
        label=make_label(item.get("formatted"), name, address, lat, lon),
        provider="geoapify",
        name=name,
        kind=item.get("result_type"),
        address=address,
        provider_place_id=str(place_id) if place_id else None,
        confidence=float(confidence) if isinstance(confidence, int | float) else None,
    )


def _results(data: Any) -> list[Place]:
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        raise BAD_RESPONSE
    return [_place(item) for item in data["results"]]


class GeoapifyProvider:
    name = "geoapify"
    attribution = ATTRIBUTION

    def __init__(self, base_url: str, api_key: str, client: UpstreamClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.client = client

    async def search(self, p: SearchParams, deadline: float) -> list[Place]:
        params: dict[str, Any] = {"text": p.q, "format": "json", "limit": p.limit, "apiKey": self.api_key}
        filters = []
        if p.countrycodes:
            filters.append("countrycode:" + ",".join(p.countrycodes))
        if p.viewbox:
            filters.append("rect:" + ",".join(f"{v:.6f}" for v in p.viewbox))
        if filters:
            params["filter"] = "|".join(filters)
        if p.lang:
            params["lang"] = p.lang
        return _results(await self.client.get_json(f"{self.base_url}/v1/geocode/search", params, deadline))

    async def reverse(self, p: ReverseParams, deadline: float) -> list[Place]:
        params: dict[str, Any] = {
            "lat": f"{p.lat:.6f}",
            "lon": f"{p.lon:.6f}",
            "format": "json",
            "limit": 1,
            "apiKey": self.api_key,
        }
        if p.lang:
            params["lang"] = p.lang
        return _results(await self.client.get_json(f"{self.base_url}/v1/geocode/reverse", params, deadline))
