"""Parsing and validating request parameters.

Inputs are normalised only where that cannot change their meaning: surrounding
and repeated whitespace is collapsed, country codes are lower-cased, sorted
and de-duplicated, and coordinates are rounded to six decimal places (about
11 cm). The same normalised values are sent upstream and used for the cache
key, so a cached answer always matches the request that produced it.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

from fastapi import Request

from app.errors import ApiError

MAX_QUERY_LENGTH = 200
MAX_COUNTRIES = 10
COUNTRY = re.compile(r"^[a-z]{2}$")
LANGUAGE = re.compile(r"^[a-z]{2}$")

SEARCH_PARAMS = {"q", "limit", "countrycodes", "viewbox", "lang"}
REVERSE_PARAMS = {"lat", "lon", "lang"}
HINTS = {
    "zoom": "zoom is no longer supported; reverse lookups return the closest address.",
    "countries": "use countrycodes.",
    "bounded": "viewbox always restricts results to the box.",
}


def invalid(message: str) -> ApiError:
    return ApiError(400, "invalid_request", message)


@dataclass(frozen=True)
class SearchParams:
    q: str
    limit: int
    countrycodes: tuple[str, ...]
    viewbox: tuple[float, float, float, float] | None
    lang: str | None

    def echo(self) -> dict[str, object]:
        return {
            "q": self.q,
            "limit": self.limit,
            "countrycodes": list(self.countrycodes),
            "viewbox": list(self.viewbox) if self.viewbox else None,
            "lang": self.lang,
        }


@dataclass(frozen=True)
class ReverseParams:
    lat: float
    lon: float
    lang: str | None

    def echo(self) -> dict[str, object]:
        return {"lat": self.lat, "lon": self.lon, "lang": self.lang}


def _reject_unknown(request: Request, allowed: set[str]) -> None:
    for name in request.query_params:
        if name not in allowed:
            hint = HINTS.get(name, f"allowed parameters are: {', '.join(sorted(allowed))}.")
            raise invalid(f"Unknown parameter '{name}': {hint}")
    for name in allowed:
        if len(request.query_params.getlist(name)) > 1:
            raise invalid(f"Parameter '{name}' was given more than once.")


def normalise_query(raw: str | None) -> str:
    if raw is None:
        raise invalid("Parameter 'q' is required.")
    text = unicodedata.normalize("NFC", raw)
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise invalid("Parameter 'q' must not contain control characters.")
    text = " ".join(text.split())
    if not text:
        raise invalid("Parameter 'q' must not be empty.")
    if len(text) > MAX_QUERY_LENGTH:
        raise invalid(f"Parameter 'q' must be at most {MAX_QUERY_LENGTH} characters.")
    return text


def parse_number(name: str, raw: str | None, low: float, high: float) -> float:
    if raw is None or not raw.strip():
        raise invalid(f"Parameter '{name}' is required.")
    try:
        value = float(raw)
    except ValueError:
        raise invalid(f"Parameter '{name}' must be a number.") from None
    if not math.isfinite(value):
        raise invalid(f"Parameter '{name}' must be a finite number.")
    if not low <= value <= high:
        raise invalid(f"Parameter '{name}' must be between {low:g} and {high:g}.")
    return round(value, 6)


def parse_limit(raw: str | None, maximum: int) -> int:
    if raw is None:
        return min(5, maximum)
    if not raw.isdigit():
        raise invalid("Parameter 'limit' must be a whole number.")
    value = int(raw)
    if not 1 <= value <= maximum:
        raise invalid(f"Parameter 'limit' must be between 1 and {maximum}.")
    return value


def parse_countries(raw: str | None) -> tuple[str, ...]:
    if raw is None:
        return ()
    codes = [c.strip().lower() for c in raw.split(",") if c.strip()]
    if not codes:
        raise invalid("Parameter 'countrycodes' must list at least one code.")
    bad = [c for c in codes if not COUNTRY.match(c)]
    if bad:
        raise invalid(f"Country codes must be two-letter ISO 3166-1 codes; got '{bad[0]}'.")
    unique = tuple(sorted(set(codes)))
    if len(unique) > MAX_COUNTRIES:
        raise invalid(f"At most {MAX_COUNTRIES} country codes are allowed.")
    return unique


def parse_viewbox(raw: str | None) -> tuple[float, float, float, float] | None:
    if raw is None:
        return None
    parts = raw.split(",")
    if len(parts) != 4:
        raise invalid("Parameter 'viewbox' must be min_lon,min_lat,max_lon,max_lat.")
    min_lon = parse_number("viewbox min_lon", parts[0], -180, 180)
    min_lat = parse_number("viewbox min_lat", parts[1], -90, 90)
    max_lon = parse_number("viewbox max_lon", parts[2], -180, 180)
    max_lat = parse_number("viewbox max_lat", parts[3], -90, 90)
    if min_lon >= max_lon or min_lat >= max_lat:
        raise invalid(
            "Parameter 'viewbox' must be min_lon,min_lat,max_lon,max_lat with min values smaller than max values. "
            "Boxes that cross the antimeridian are not supported."
        )
    return (min_lon, min_lat, max_lon, max_lat)


def parse_language(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if not LANGUAGE.match(value):
        raise invalid("Parameter 'lang' must be a two-letter ISO 639-1 code such as 'en'.")
    return value


def search_params(request: Request, max_limit: int) -> SearchParams:
    _reject_unknown(request, SEARCH_PARAMS)
    p = request.query_params
    return SearchParams(
        q=normalise_query(p.get("q")),
        limit=parse_limit(p.get("limit"), max_limit),
        countrycodes=parse_countries(p.get("countrycodes")),
        viewbox=parse_viewbox(p.get("viewbox")),
        lang=parse_language(p.get("lang")),
    )


def reverse_params(request: Request) -> ReverseParams:
    _reject_unknown(request, REVERSE_PARAMS)
    p = request.query_params
    return ReverseParams(
        lat=parse_number("lat", p.get("lat"), -90, 90),
        lon=parse_number("lon", p.get("lon"), -180, 180),
        lang=parse_language(p.get("lang")),
    )
