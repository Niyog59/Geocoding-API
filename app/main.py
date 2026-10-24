"""Application factory."""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.cache import MemoryCache
from app.config import Settings
from app.keys import KeyStore, hash_api_key
from app.logging_setup import configure_logging
from app.providers.nominatim import NominatimClient
from app.routes import build_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    logger = configure_logging()
    keys = KeyStore(settings.master_api_key)
    cache = MemoryCache(settings.cache_ttl)
    provider = NominatimClient(settings.nominatim_base_url, settings.user_agent)

    def rate_limit_key(request: Request) -> str:
        api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
        if api_key and keys.validate(api_key):
            return f"apikey:{hash_api_key(api_key)[:16]}"
        return get_remote_address(request)

    limiter = Limiter(key_func=rate_limit_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        keys.init_default_keys()
        logger.info("Geocoding API started successfully")
        yield

    app = FastAPI(
        title="Nominatim Geocoding API",
        description="A production-ready, Mapbox-compatible geocoding API.\n\n"
        "Demo key for testing: `geo_demo_key_for_testing_only`",
        version="2.0.0",
        lifespan=lifespan,
    )
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "X-RateLimit-Limit", "X-RateLimit-Remaining"],
    )

    @app.middleware("http")
    async def add_request_id(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        request_id = request.headers.get("X-Request-ID") or f"req_{uuid.uuid4().hex[:12]}"
        request.state.request_id = request_id
        start = time.time()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{time.time() - start:.3f}s"
        return response

    app.include_router(build_router(settings, limiter, keys, cache, provider))
    return app
