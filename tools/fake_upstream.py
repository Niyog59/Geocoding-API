"""Fake geocoding provider for tests and benchmarks.

Emulates the small part of the Geoapify and Nominatim HTTP APIs that this
project calls. Results are deterministic, so tests can compare them, and the
server never contacts a real provider.

Run it on its own for local development or benchmarks:

    uvicorn tools.fake_upstream:app --port 9100

Behaviour can be changed at runtime through the /__control endpoints, which is
how the integration tests and the benchmark simulate slow or failing providers.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response


@dataclass
class FakeState:
    """Mutable behaviour shared by all fake endpoints."""

    delay_seconds: float = float(os.getenv("FAKE_UPSTREAM_DELAY_MS", "0")) / 1000
    # ok | empty | status | malformed | hang
    mode: str = "ok"
    status_code: int = 500
    retry_after: str | None = None
    # When set, only this many next requests use `mode`; later ones are "ok".
    remaining_mode_requests: int | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def reset(self) -> None:
        self.delay_seconds = 0.0
        self.mode = "ok"
        self.status_code = 500
        self.retry_after = None
        self.remaining_mode_requests = None
        self.calls.clear()

    def current_mode(self) -> str:
        if self.remaining_mode_requests is None:
            return self.mode
        if self.remaining_mode_requests <= 0:
            return "ok"
        self.remaining_mode_requests -= 1
        return self.mode


def _digest(text: str) -> int:
    return int(hashlib.sha256(text.encode()).hexdigest()[:12], 16)


def _point_for(text: str, rect: tuple[float, float, float, float] | None) -> tuple[float, float]:
    """Deterministic coordinates for a query, inside `rect` when one is given."""
    h = _digest(text)
    if rect:
        min_lon, min_lat, max_lon, max_lat = rect
        lon = min_lon + (max_lon - min_lon) * ((h % 1000) / 1000)
        lat = min_lat + (max_lat - min_lat) * (((h // 1000) % 1000) / 1000)
    else:
        lon = -170 + (h % 34000) / 100
        lat = -60 + ((h // 34000) % 12000) / 100
    return round(lon, 6), round(lat, 6)


def _parse_rect(value: str | None) -> tuple[float, float, float, float] | None:
    if not value:
        return None
    parts = [float(p) for p in value.split(",")]
    lon1, lat1, lon2, lat2 = parts
    return min(lon1, lon2), min(lat1, lat2), max(lon1, lon2), max(lat1, lat2)


def _place(text: str, index: int, rect: tuple[float, float, float, float] | None, country: str) -> dict[str, Any]:
    lon, lat = _point_for(f"{text}#{index}", rect)
    name = text.title() if index == 0 else f"{text.title()} {index + 1}"
    return {
        "name": name,
        "street": "Main Street",
        "housenumber": str(100 + index),
        "city": "Exampleville",
        "state": "Example State",
        "postcode": "00000",
        "country": "Exampleland",
        "country_code": country,
        "lat": lat,
        "lon": lon,
        "place_id": f"fake{_digest(f'{text}#{index}#{rect}'):x}",
        "result_type": "building",
        "rank": {"confidence": 0.9 if index == 0 else 0.5},
    }


def create_fake_upstream(state: FakeState | None = None) -> FastAPI:
    state = state or FakeState()
    fake = FastAPI(title="Fake geocoding provider")
    fake.state.fake = state

    async def behave(request: Request) -> Response | None:
        state.calls.append({"path": request.url.path, "params": dict(request.query_params)})
        if state.delay_seconds:
            await asyncio.sleep(state.delay_seconds)
        mode = state.current_mode()
        if mode == "status":
            headers = {"Retry-After": state.retry_after} if state.retry_after else None
            return JSONResponse({"error": "fake failure"}, status_code=state.status_code, headers=headers)
        if mode == "malformed":
            return PlainTextResponse("<html>not json</html>", status_code=200)
        if mode == "hang":
            await asyncio.sleep(3600)
        if mode == "empty":
            return JSONResponse({"results": []} if request.url.path.startswith("/v1/") else [])
        return None

    # ---- Geoapify-style endpoints -------------------------------------------------

    @fake.get("/v1/geocode/search")
    async def geoapify_search(request: Request) -> Response:
        early = await behave(request)
        if early is not None:
            return early
        p = request.query_params
        text = p.get("text", "")
        if text.lower().startswith("nowhere"):
            return JSONResponse({"results": []})
        rect = None
        country = "xx"
        for part in (p.get("filter") or "").split("|"):
            if part.startswith("rect:"):
                rect = _parse_rect(part[5:])
            if part.startswith("countrycode:"):
                country = part[12:].split(",")[0]
        limit = int(p.get("limit", "5"))
        results = [_place(text, i, rect, country) for i in range(min(limit, 3))]
        for r in results:
            r["formatted"] = f"{r['name']}, {r['housenumber']} {r['street']}, {r['city']}"
            r["address_line1"] = r["name"]
            r["address_line2"] = f"{r['housenumber']} {r['street']}, {r['city']}"
        if text.lower().startswith("unlabeled"):
            for r in results:
                for key in ("formatted", "address_line1", "address_line2", "name", "place_id"):
                    r.pop(key, None)
        return JSONResponse({"results": results, "query": {"text": text}})

    @fake.get("/v1/geocode/reverse")
    async def geoapify_reverse(request: Request) -> Response:
        early = await behave(request)
        if early is not None:
            return early
        lat = float(request.query_params["lat"])
        lon = float(request.query_params["lon"])
        if lat > 80:  # treat the far north as open ocean with no address
            return JSONResponse({"results": []})
        place = _place(f"{lat},{lon}", 0, None, "xx")
        place.update({"lat": lat, "lon": lon})
        place["formatted"] = f"{place['housenumber']} {place['street']}, {place['city']}"
        return JSONResponse({"results": [place]})

    # ---- Nominatim-style endpoints ------------------------------------------------

    def nominatim_item(place: dict[str, Any], with_display: bool = True) -> dict[str, Any]:
        item: dict[str, Any] = {
            "place_id": place.get("place_id"),
            "lat": str(place["lat"]),
            "lon": str(place["lon"]),
            "address": {
                "road": place["street"],
                "house_number": place["housenumber"],
                "city": place["city"],
                "state": place["state"],
                "postcode": place["postcode"],
                "country": place["country"],
                "country_code": place["country_code"],
            },
        }
        if with_display:
            item["display_name"] = f"{place['housenumber']} {place['street']}, {place['city']}"
            item["name"] = place["name"]
        return item

    @fake.get("/search")
    async def nominatim_search(request: Request) -> Response:
        early = await behave(request)
        if early is not None:
            return early
        p = request.query_params
        text = p.get("q", "")
        if text.lower().startswith("nowhere"):
            return JSONResponse([])
        rect = _parse_rect(p.get("viewbox")) if p.get("bounded") == "1" else None
        country = (p.get("countrycodes") or "xx").split(",")[0]
        limit = int(p.get("limit", "5"))
        unlabeled = text.lower().startswith("unlabeled")
        items = []
        for i in range(min(limit, 3)):
            item = nominatim_item(_place(text, i, rect, country), with_display=not unlabeled)
            if unlabeled:
                item.pop("place_id")
            items.append(item)
        return JSONResponse(items)

    @fake.get("/reverse")
    async def nominatim_reverse(request: Request) -> Response:
        early = await behave(request)
        if early is not None:
            return early
        lat = float(request.query_params["lat"])
        lon = float(request.query_params["lon"])
        if lat > 80:
            return JSONResponse({"error": "Unable to geocode"})
        place = _place(f"{lat},{lon}", 0, None, "xx")
        place.update({"lat": lat, "lon": lon})
        return JSONResponse(nominatim_item(place))

    # ---- Control endpoints (tests and benchmarks only) -------------------------------

    @fake.get("/__control/state")
    async def get_state() -> dict[str, Any]:
        return {"calls": len(state.calls), "mode": state.mode, "delay_seconds": state.delay_seconds}

    @fake.post("/__control/reset")
    async def reset() -> dict[str, str]:
        state.reset()
        return {"status": "reset"}

    @fake.post("/__control/configure")
    async def configure(request: Request) -> dict[str, Any]:
        body = await request.json()
        for key in ("delay_seconds", "mode", "status_code", "retry_after", "remaining_mode_requests"):
            if key in body:
                setattr(state, key, body[key])
        return {"status": "configured"}

    return fake


app = create_fake_upstream()
