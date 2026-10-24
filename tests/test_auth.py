"""Authentication and key management."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import KEY_PATTERN, generate_key
from app.config import ConfigError, Settings
from tests.support import ADMIN_SECRET, make_app

ADMIN = {"X-Admin-Secret": ADMIN_SECRET}
PROVIDER = {"provider": "geoapify", "geoapify_api_key": "k" * 20}
GOOD_PRODUCTION = {
    **PROVIDER,
    "app_env": "production",
    "admin_secret": "s" * 40,
    "redis_url": "rediss://db.example:6379",
}


def test_generated_keys_are_random_and_well_formed():
    keys = {generate_key()[1] for _ in range(50)}
    assert len(keys) == 50
    assert all(KEY_PATTERN.match(k) for k in keys)


def test_missing_key_is_rejected_when_required(redis_url):
    app, _ = make_app(redis_url)
    with TestClient(app) as client:
        r = client.get("/geocode", params={"q": "Berlin"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "missing_api_key"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x", "rate_per_minute": 0},
        {"name": "x", "rate_per_minute": 100_000},
        {"name": "x", "daily_limit": -1},
        {"name": ""},
    ],
)
def test_key_limits_are_validated(redis_url, body):
    app, _ = make_app(redis_url)
    with TestClient(app) as client:
        r = client.post("/admin/keys", headers=ADMIN, json=body)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_request"


def test_new_key_uses_requested_limits_and_appears_in_listing(redis_url):
    app, _ = make_app(redis_url)
    with TestClient(app) as client:
        created = client.post(
            "/admin/keys", headers=ADMIN, json={"name": "app", "rate_per_minute": 7, "daily_limit": 70}
        ).json()
        listed = client.get("/admin/keys", headers=ADMIN).json()["keys"]
        assert [(k["id"], k["rate_per_minute"], k["daily_limit"]) for k in listed] == [(created["id"], 7, 70)]
        assert "api_key" not in listed[0]


def test_admin_routes_are_disabled_without_a_secret(redis_url):
    app, _ = make_app(redis_url, admin_secret=None)
    with TestClient(app) as client:
        r = client.get("/admin/keys", headers={"X-Admin-Secret": "anything"})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "admin_disabled"


def test_revoking_an_unknown_key_returns_404(redis_url):
    app, _ = make_app(redis_url)
    with TestClient(app) as client:
        assert client.delete("/admin/keys/000000000000", headers=ADMIN).status_code == 404


def test_valid_production_configuration_is_accepted():
    assert Settings(**GOOD_PRODUCTION).app_env == "production"


@pytest.mark.parametrize("secret", [None, "short", "change-me-" + "x" * 40])
def test_production_requires_a_strong_admin_secret(secret):
    with pytest.raises(ValueError, match="ADMIN_SECRET"):
        Settings(**{**GOOD_PRODUCTION, "admin_secret": secret})


def test_production_rejects_local_redis():
    with pytest.raises(ValueError, match="localhost"):
        Settings(**{**GOOD_PRODUCTION, "redis_url": "redis://localhost:6379/0"})


def test_wildcard_cors_is_rejected():
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        Settings(**PROVIDER, cors_origins="*")


def test_config_error_is_a_value_error():
    assert issubclass(ConfigError, ValueError)
