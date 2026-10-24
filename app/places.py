"""Provider-neutral results and their GeoJSON form.

Each provider adapter turns its own response into `Place` objects. Everything
after that point (caching, IDs, labels, the response body) works the same way
for every provider.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from typing import Any

ADDRESS_ORDER = ("housenumber", "street", "suburb", "city", "county", "state", "postcode", "country")


@dataclass(frozen=True)
class Place:
    lat: float
    lon: float
    label: str
    provider: str
    name: str | None = None
    kind: str | None = None
    address: dict[str, str] = field(default_factory=dict)
    provider_place_id: str | None = None
    # Only set when the provider supplies a score. This service never invents one.
    confidence: float | None = None


def make_label(formatted: str | None, name: str | None, address: dict[str, str], lat: float, lon: float) -> str:
    """Best available human-readable label, falling back step by step."""
    if formatted and formatted.strip():
        return formatted.strip()
    street = " ".join(p for p in (address.get("housenumber"), address.get("street")) if p)
    parts = [p for p in (name, street, address.get("city"), address.get("state"), address.get("country")) if p]
    if parts:
        return ", ".join(dict.fromkeys(parts))
    return f"{lat:.6f}, {lon:.6f}"


def feature_id(place: Place) -> str:
    """A stable ID: the provider's own ID when it has one, otherwise a content hash.

    Python's built-in hash() is randomised per process, so it must not be used here.
    """
    if place.provider_place_id:
        return f"{place.provider}:{place.provider_place_id}"
    digest = hashlib.sha256(f"{place.provider}|{place.lat:.6f}|{place.lon:.6f}|{place.label}".encode()).hexdigest()
    return f"{place.provider}:h{digest[:16]}"


def to_feature(place: Place) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "label": place.label,
        "name": place.name,
        "kind": place.kind,
        "address": {k: place.address[k] for k in (*ADDRESS_ORDER, "country_code") if place.address.get(k)},
        "provider": place.provider,
        "provider_place_id": place.provider_place_id,
    }
    if place.confidence is not None:
        properties["confidence"] = place.confidence
    return {
        "type": "Feature",
        "id": feature_id(place),
        # GeoJSON order is longitude, latitude.
        "geometry": {"type": "Point", "coordinates": [place.lon, place.lat]},
        "properties": properties,
    }


def place_to_dict(place: Place) -> dict[str, Any]:
    return asdict(place)
