"""Conversion of Nominatim results into GeoJSON features."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def nominatim_to_feature(item: dict[str, Any], is_reverse: bool = False) -> dict[str, Any]:
    try:
        lon = float(item["lon"])
        lat = float(item["lat"])
    except (KeyError, ValueError, TypeError):
        raise HTTPException(status_code=502, detail="Invalid response from geocoding service") from None

    address = item.get("address", {})
    display_name = item.get("display_name", "")
    place_id = item.get("place_id", hash(display_name))

    place_type = ["place"]
    if "house_number" in address or "road" in address:
        place_type = ["address"]
    elif "city" in address or "town" in address or "village" in address:
        place_type = ["place"]
    elif "state" in address or "county" in address:
        place_type = ["region"]
    elif "country" in address:
        place_type = ["country"]

    text = (
        address.get("name")
        or address.get("road")
        or address.get("city")
        or address.get("town")
        or address.get("village")
        or display_name.split(",")[0]
        if display_name
        else "Unknown"
    )

    context = []
    for address_key, context_type in [
        ("postcode", "postcode"),
        ("city", "place"),
        ("town", "place"),
        ("village", "place"),
        ("county", "district"),
        ("state", "region"),
        ("country", "country"),
    ]:
        if address_key in address:
            context.append({"id": f"{context_type}.{place_id}", "text": address[address_key]})

    return {
        "id": f"place.{place_id}",
        "type": "Feature",
        "place_type": place_type,
        "relevance": 1.0,
        "properties": {"accuracy": "point"},
        "text": text,
        "place_name": display_name,
        "center": [lon, lat],
        "geometry": {"type": "Point", "coordinates": [lon, lat]},
        "context": context,
    }


def nominatim_to_autocomplete(item: dict[str, Any]) -> dict[str, Any] | None:
    try:
        lon = float(item["lon"])
        lat = float(item["lat"])
    except (KeyError, ValueError, TypeError):
        return None
    display_name = item.get("display_name", "")
    place_id = item.get("place_id", hash(display_name))
    address = item.get("address", {})
    text = (
        address.get("name") or address.get("road") or address.get("city") or display_name.split(",")[0]
        if display_name
        else "Unknown"
    )
    return {"id": f"place.{place_id}", "text": text, "place_name": display_name, "center": [lon, lat]}
