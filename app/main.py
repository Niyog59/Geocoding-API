"""Application factory."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.auth import KeyStore
from app.cache import ResponseCache
from app.config import Settings
from app.errors import error_response, install_error_handlers
from app.limits import ProviderBudget, RateLimiter
from app.logging_setup import configure_logging, request_id_var
from app.metrics import HTTP_DURATION, HTTP_REQUESTS, LIMIT_REJECTIONS
from app.providers.nominatim import NominatimProvider
from app.routes import admin, geocoding, health
from app.service import GeocodingService
from app.upstream import UpstreamClient

logger = logging.getLogger("app")

# Query parameter names that look like credentials. Credentials in URLs end up
# in browser history, proxy logs and analytics, so they are refused outright.
URL_CREDENTIAL_PARAMS = {"api_key", "apikey", "key", "master_key", "admin_secret", "token", "access_token"}


def create_app(settings: Settings | None = None, upstream_transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, secrets=[settings.admin_secret or ""])

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = redis.Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_timeout=settings.redis_timeout_seconds,
            socket_connect_timeout=settings.redis_timeout_seconds,
        )
        budget = ProviderBudget(client, "nominatim", settings.provider_daily_budget, settings.provider_rate_per_second)
        upstream = UpstreamClient(
            "nominatim",
            budget,
            connect_timeout=settings.upstream_connect_timeout,
            read_timeout=settings.upstream_read_timeout,
            concurrency=settings.upstream_concurrency,
            queue_seconds=settings.upstream_queue_seconds,
            max_retries=settings.upstream_max_retries,
            max_retry_after=settings.upstream_max_retry_after,
            budget_wait_seconds=settings.provider_wait_seconds,
            headers={"User-Agent": settings.user_agent, "Accept": "application/json"},
            transport=upstream_transport,
        )
        provider = NominatimProvider(settings.nominatim_base_url, upstream)
        cache = ResponseCache(
            client,
            ttl_seconds=settings.cache_ttl_seconds,
            empty_ttl_seconds=settings.empty_cache_ttl_seconds,
            max_entries=settings.cache_max_entries,
            max_entry_bytes=settings.cache_max_entry_bytes,
            lock_seconds=settings.coalesce_lock_seconds,
            wait_seconds=settings.coalesce_wait_seconds,
        )
        app.state.redis = client
        app.state.keys = KeyStore(client)
        app.state.limiter = RateLimiter(client, settings.usage_retention_days)
        app.state.service = GeocodingService(provider, cache, settings.request_deadline_seconds)
        logger.info("Geocoding API started (provider: %s)", provider.name)
        try:
            yield
        finally:
            # Uvicorn stops accepting connections and lets in-flight requests
            # finish before this runs; then the shared clients are closed.
            await upstream.aclose()
            await client.aclose()
            logger.info("Geocoding API stopped")

    app = FastAPI(
        title="Geocoding API",
        version="3.0.0",
        description="Address search and reverse geocoding with GeoJSON responses. "
        "Send API keys in the `X-API-Key` header; administration uses `X-Admin-Secret`.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    install_error_handlers(app)

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        token = request_id_var.set(request_id)
        request.state.request_id = request_id
        start = time.perf_counter()
        try:
            if URL_CREDENTIAL_PARAMS.intersection(k.lower() for k in request.query_params):
                response: Response = error_response(
                    request,
                    400,
                    "credentials_in_url",
                    "Credentials are not accepted in the URL. Send them in the X-API-Key or X-Admin-Secret header.",
                )
            else:
                response = await call_next(request)
        except Exception:
            logger.exception("Unhandled error")
            response = error_response(request, 500, "internal_error", "An unexpected error occurred.")
        duration = time.perf_counter() - start
        route = getattr(request.scope.get("route"), "path", "unmatched")
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{duration:.3f}s"
        HTTP_REQUESTS.labels(route, request.method, f"{response.status_code // 100}xx").inc()
        HTTP_DURATION.labels(route).observe(duration)
        if response.status_code in (429, 503) and response.headers.get("Retry-After"):
            LIMIT_REJECTIONS.labels(str(response.status_code)).inc()
        logger.info(
            "request",
            extra={
                "route": route,
                "method": request.method,
                "status": response.status_code,
                "duration_ms": round(duration * 1000, 1),
                "cache": response.headers.get("X-Cache"),
            },
        )
        request_id_var.reset(token)
        return response

    # Added last so it wraps everything, including error responses.
    if settings.origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.origins,
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["X-API-Key"],
            expose_headers=[
                "X-Request-ID",
                "X-Response-Time",
                "X-Cache",
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                "Retry-After",
            ],
            max_age=600,
        )

    app.include_router(health.router)
    app.include_router(admin.router)
    app.include_router(geocoding.router)
    return app
