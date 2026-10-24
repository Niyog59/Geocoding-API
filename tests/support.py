"""Builds an application instance wired to the in-process fake provider."""

from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings
from app.main import create_app
from tools.fake_upstream import FakeState, create_fake_upstream

ADMIN_SECRET = "test-admin-secret-" + "x" * 32


def make_app(redis_url: str, state: FakeState | None = None, **settings: Any):
    state = state or FakeState()
    values: dict[str, Any] = {
        "app_env": "test",
        "redis_url": redis_url,
        "admin_secret": ADMIN_SECRET,
        "nominatim_base_url": "http://fake-upstream",
        "provider_rate_per_second": 1000,
    }
    values.update({k: v for k, v in settings.items() if k in Settings.model_fields})
    config = Settings(**values)
    transport = httpx.ASGITransport(app=create_fake_upstream(state))
    return create_app(config, upstream_transport=transport), state
