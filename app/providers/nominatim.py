"""Adapter for a Nominatim server.

Use only an instance you are allowed to use for this purpose, such as your own.
The public nominatim.openstreetmap.org service forbids autocomplete and heavy
or redistributed use, so it is not a default.
"""

from __future__ import annotations

from typing import Any

from app.errors import ApiError
from app.places import Place, make_label
from app.upstream import UpstreamClient
from app.validation import ReverseParams, SearchParams

ATTRIBUTION = [{"text": "© OpenStreetMap contributors", "url": "https://www.openstreetmap.org/copyright"}]
BAD_RESPONSE = ApiError(502, "upstream_bad_response", "The geocoding provider sent an unreadable response.")


def _place(item: Any) -> Place:
    if not isinstance(item, dict):
        raise BAD_RESPONSE
    try:
        lat = float(item["lat"])
        lon = float(item["lon"])
    except (KeyError, ValueError, TypeError):
        raise BAD_RESPONSE from None
    raw = item.get("address") or {}
    fields = {
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
    address = {k: str(v) for k, v in fields.items() if v}
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

    def __init__(self, base_url: str, client: UpstreamClient) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client

    async def search(self, p: SearchParams, deadline: float) -> list[Place]:
        params: dict[str, Any] = {"q": p.q, "format": "jsonv2", "limit": p.limit, "addressdetails": 1}
        if p.countrycodes:
            params["countrycodes"] = ",".join(p.countrycodes)
        if p.viewbox:
            params["viewbox"] = ",".join(f"{v:.6f}" for v in p.viewbox)
            params["bounded"] = 1
        if p.lang:
            params["accept-language"] = p.lang
        data = await self.client.get_json(f"{self.base_url}/search", params, deadline)
        if not isinstance(data, list):
            raise BAD_RESPONSE
        return [_place(item) for item in data]

    async def reverse(self, p: ReverseParams, deadline: float) -> list[Place]:
        params: dict[str, Any] = {"lat": f"{p.lat:.6f}", "lon": f"{p.lon:.6f}", "format": "jsonv2", "addressdetails": 1}
        if p.lang:
            params["accept-language"] = p.lang
        data = await self.client.get_json(f"{self.base_url}/reverse", params, deadline)
        if isinstance(data, dict) and "error" in data:
            return []  # Nominatim reports "no address here" this way.
        return [_place(data)]
