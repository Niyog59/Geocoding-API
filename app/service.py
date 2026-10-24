"""Search and reverse lookup: cache first, then the provider within its budget."""

from __future__ import annotations

import time
from typing import Any

from app.cache import CacheStatus, ResponseCache, cache_key
from app.metrics import CACHE_LOOKUPS
from app.places import to_feature
from app.validation import ReverseParams, SearchParams

CACHE_FORMAT_VERSION = 1


class GeocodingService:
    def __init__(self, provider: Any, cache: ResponseCache, deadline_seconds: float) -> None:
        self.provider = provider
        self.cache = cache
        self.deadline_seconds = deadline_seconds

    def _deadline(self) -> float:
        return time.monotonic() + self.deadline_seconds

    def _collection(self, query: dict[str, object], features: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "type": "FeatureCollection",
            "query": query,
            "features": features,
            "attribution": self.provider.attribution,
        }

    async def search(self, p: SearchParams) -> tuple[dict[str, Any], CacheStatus]:
        identity = {
            "v": CACHE_FORMAT_VERSION,
            "op": "search",
            "provider": self.provider.name,
            # Lookups are case-insensitive, so case is folded for the key only.
            "q": p.q.casefold(),
            "limit": p.limit,
            "countrycodes": list(p.countrycodes),
            "viewbox": list(p.viewbox) if p.viewbox else None,
            "lang": p.lang,
        }

        async def fetch() -> list[dict[str, Any]]:
            return [to_feature(place) for place in await self.provider.search(p, self._deadline())]

        features, status = await self.cache.get_or_fetch(cache_key(identity), fetch)
        CACHE_LOOKUPS.labels("search", status).inc()
        return self._collection(p.echo(), features), status

    async def reverse(self, p: ReverseParams) -> tuple[dict[str, Any], CacheStatus]:
        identity = {
            "v": CACHE_FORMAT_VERSION,
            "op": "reverse",
            "provider": self.provider.name,
            "lat": p.lat,
            "lon": p.lon,
            "lang": p.lang,
        }

        async def fetch() -> list[dict[str, Any]]:
            return [to_feature(place) for place in await self.provider.reverse(p, self._deadline())]

        features, status = await self.cache.get_or_fetch(cache_key(identity), fetch)
        CACHE_LOOKUPS.labels("reverse", status).inc()
        return self._collection(p.echo(), features), status
