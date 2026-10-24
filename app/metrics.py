"""Prometheus metrics.

Labels are limited to a small fixed set of values (route templates, status
classes, outcomes). Raw queries, coordinates, API keys and request IDs are
never used as labels, because each distinct value creates a new time series.

With several worker processes, set PROMETHEUS_MULTIPROC_DIR so that /metrics
reports all workers together; otherwise each scrape sees only the worker that
answered it.
"""

from __future__ import annotations

import os

from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Histogram, generate_latest, multiprocess

HTTP_REQUESTS = Counter("geo_http_requests_total", "HTTP requests handled", ["route", "method", "status_class"])
HTTP_DURATION = Histogram(
    "geo_http_request_duration_seconds",
    "Time to handle HTTP requests",
    ["route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5, 10),
)
UPSTREAM_REQUESTS = Counter("geo_upstream_requests_total", "Calls to the geocoding provider", ["provider", "outcome"])
UPSTREAM_DURATION = Histogram(
    "geo_upstream_request_duration_seconds",
    "Duration of calls to the geocoding provider",
    ["provider"],
    buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 1, 2, 5, 10),
)
CACHE_LOOKUPS = Counter("geo_cache_lookups_total", "Cache lookups by outcome", ["operation", "outcome"])
LIMIT_REJECTIONS = Counter("geo_limit_rejections_total", "Requests refused by limits or quotas", ["code"])


def render() -> tuple[bytes, str]:
    if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)  # type: ignore[no-untyped-call]
        return generate_latest(registry), CONTENT_TYPE_LATEST
    return generate_latest(), CONTENT_TYPE_LATEST
