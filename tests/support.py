"""Builds an application instance wired to the in-process fake provider.

Tests describe settings with the names used by the improved service. This
version targets the original single-file `main.py`, so settings it has no
equivalent for are ignored and the corresponding tests are expected to fail.
"""

from __future__ import annotations

import importlib
import os
import sys
from functools import partial
from typing import Any

import httpx

from tools.fake_upstream import FakeState, create_fake_upstream

ADMIN_SECRET = "test-admin-secret-" + "x" * 32


def make_app(redis_url: str, state: FakeState | None = None, **settings: Any):
    state = state or FakeState()
    env = {
        "NOMINATIM_BASE_URL": "http://fake-upstream",
        "REQUIRE_API_KEY": "true" if settings.get("require_api_key", True) else "false",
        "MASTER_API_KEY": settings.get("admin_secret", ADMIN_SECRET),
        "RATE_LIMIT": "1000/minute",
        "RATE_LIMIT_AUTHENTICATED": "1000/minute",
    }
    os.environ.update(env)
    sys.modules.pop("main", None)
    main = importlib.import_module("main")
    transport = httpx.ASGITransport(app=create_fake_upstream(state))
    main.httpx.AsyncClient = partial(httpx.AsyncClient, transport=transport)  # type: ignore[attr-defined]
    return main.app, state
