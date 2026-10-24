"""Builds an application instance wired to the in-process fake provider.

Tests describe settings with the names used by the improved service. Settings
the current code has no equivalent for are ignored, and the tests that depend
on them are expected to fail until the matching fix lands.
"""

from __future__ import annotations

from functools import partial
from typing import Any

import httpx

import app.providers.nominatim as nominatim_module
from app.config import Settings
from app.main import create_app
from tools.fake_upstream import FakeState, create_fake_upstream

ADMIN_SECRET = "test-admin-secret-" + "x" * 32


def make_app(redis_url: str, state: FakeState | None = None, **settings: Any):
    state = state or FakeState()
    config = Settings(
        nominatim_base_url="http://fake-upstream",
        require_api_key=settings.get("require_api_key", True),
        master_api_key=settings.get("admin_secret", ADMIN_SECRET),
        rate_limit="1000/minute",
        rate_limit_authenticated="1000/minute",
    )
    transport = httpx.ASGITransport(app=create_fake_upstream(state))
    nominatim_module.httpx.AsyncClient = partial(httpx.AsyncClient, transport=transport)  # type: ignore[attr-defined]
    return create_app(config), state
