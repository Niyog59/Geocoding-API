"""Application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import redis.asyncio as redis
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from app.auth import KeyStore
from app.cache import ResponseCache
from app.config import Settings
from app.errors import error_response, install_error_handlers
from app.limits import ProviderBudget, RateLimiter
from app.logging_setup import configure_logging
from app.providers.nominatim import NominatimProvider
from app.routes import admin, geocoding
from app.service import GeocodingService

# Query parameter names that look like credentials. Credentials in URLs end up
# in browser history, proxy logs and analytics, so they are refused outright.
URL_CREDENTIAL_PARAMS = {"api_key", "apikey", "key", "master_key", "admin_secret", "token", "access_token"}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logger = configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        app.state.redis = client
        app.state.keys = KeyStore(client)
        app.state.limiter = RateLimiter(client, settings.usage_retention_days)
        provider = NominatimProvider(settings.nominatim_base_url, settings.user_agent)
        budget = ProviderBudget(client, provider.name, settings.provider_daily_budget, settings.provider_rate_per_second)
        cache = ResponseCache(
            client,
            ttl_seconds=settings.cache_ttl_seconds,
            empty_ttl_seconds=settings.empty_cache_ttl_seconds,
            max_entries=settings.cache_max_entries,
            max_entry_bytes=settings.cache_max_entry_bytes,
            lock_seconds=settings.coalesce_lock_seconds,
            wait_seconds=settings.coalesce_wait_seconds,
        )
        app.state.service = GeocodingService(provider, cache, budget, settings.provider_wait_seconds)
        logger.info("Geocoding API started")
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="Geocoding API", version="3.0.0", lifespan=lifespan)
    app.state.settings = settings
    install_error_handlers(app)

    if settings.origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.origins,
            allow_credentials=False,
            allow_methods=["GET"],
            allow_headers=["X-API-Key", "X-Request-ID"],
            expose_headers=[
                "X-Request-ID",
                "X-Response-Time",
                "X-RateLimit-Limit",
                "X-RateLimit-Remaining",
                "X-RateLimit-Reset",
                "Retry-After",
            ],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        request.state.request_id = request_id
        start = time.perf_counter()
        if URL_CREDENTIAL_PARAMS.intersection(k.lower() for k in request.query_params):
            response: Response = error_response(
                request,
                400,
                "credentials_in_url",
                "Credentials are not accepted in the URL. Send them in the X-API-Key or X-Admin-Secret header.",
            )
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{time.perf_counter() - start:.3f}s"
        return response

    app.include_router(admin.router)
    app.include_router(geocoding.router)
    return app
