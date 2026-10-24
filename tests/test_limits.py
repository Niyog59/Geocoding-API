"""Rate limits, provider budget and fail-safe behaviour."""

from __future__ import annotations

import asyncio
import time

import pytest
import redis
import redis.asyncio as aredis
from fastapi.testclient import TestClient

from app.client_ip import client_ip
from app.config import Settings
from app.errors import ApiError
from app.limits import ProviderBudget
from tests.support import ADMIN_SECRET, make_app

ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


def _key(client: TestClient, **body) -> dict[str, str]:
    created = client.post("/admin/keys", headers=ADMIN, json={"name": "t", **body}).json()
    return {"X-API-Key": created["api_key"]}


def test_reset_header_and_retry_after_match_the_redis_window(redis_url):
    app, _ = make_app(redis_url)
    with TestClient(app) as client:
        headers = _key(client, rate_per_minute=1)
        first = client.get("/geocode", params={"q": "Berlin"}, headers=headers)
        blocked = client.get("/geocode", params={"q": "Berlin"}, headers=headers)
        assert first.status_code == 200 and blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"
        seconds_left_in_minute = 60 - int(time.time() % 60)
        assert abs(int(blocked.headers["Retry-After"]) - seconds_left_in_minute) <= 1
        assert blocked.headers["X-RateLimit-Remaining"] == "0"


def test_daily_limit_is_reported_when_it_is_the_tighter_window(redis_url):
    app, _ = make_app(redis_url)
    with TestClient(app) as client:
        headers = _key(client, rate_per_minute=50, daily_limit=3)
        r = client.get("/geocode", params={"q": "Berlin"}, headers=headers)
        assert (r.headers["X-RateLimit-Limit"], r.headers["X-RateLimit-Remaining"]) == ("3", "2")
        assert int(r.headers["X-RateLimit-Reset"]) > 60


def test_rejected_requests_do_not_consume_quota(redis_url):
    app, _ = make_app(redis_url)
    with TestClient(app) as client:
        headers = _key(client, rate_per_minute=2)
        for _ in range(5):
            client.get("/geocode", params={"q": "Berlin"}, headers=headers)
        usage = client.get("/usage", headers=headers).json()
        assert usage["today"]["total"] == 2


def test_demo_total_cap_is_shared_by_all_visitors(redis_url):
    app, _ = make_app(redis_url, demo_daily_total=2, client_ip_source="x-forwarded-for", trusted_proxy_count=1)
    with TestClient(app) as client:
        codes = [
            client.get("/demo/geocode", params={"q": "Berlin"}, headers={"X-Forwarded-For": f"203.0.113.{i}"})
            for i in range(3)
        ]
        assert [r.status_code for r in codes] == [200, 200, 503]
        assert codes[2].json()["error"]["code"] == "quota_exhausted"
        assert int(codes[2].headers["Retry-After"]) > 0


def test_demo_can_be_disabled(redis_url):
    app, _ = make_app(redis_url, demo_enabled=False)
    with TestClient(app) as client:
        assert client.get("/demo/geocode", params={"q": "Berlin"}).status_code == 404


def test_keyed_routes_still_require_a_key_when_demo_is_enabled(redis_url):
    app, _ = make_app(redis_url)
    with TestClient(app) as client:
        assert client.get("/demo/geocode", params={"q": "Berlin"}).status_code == 200
        assert client.get("/geocode", params={"q": "Berlin"}).status_code == 401


def test_anonymous_callers_have_their_own_limit_when_keys_are_optional(redis_url):
    app, _ = make_app(redis_url, require_api_key=False, anon_rate_per_minute=2)
    with TestClient(app) as client:
        codes = [client.get("/geocode", params={"q": "Berlin"}).status_code for _ in range(3)]
        assert codes == [200, 200, 429]


def test_redis_outage_fails_closed(redis_url):
    app, _ = make_app("redis://127.0.0.1:1/0")
    with TestClient(app) as client:
        r = client.get("/demo/geocode", params={"q": "Berlin"})
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "service_unavailable"
        r = client.get("/geocode", params={"q": "Berlin"}, headers={"X-API-Key": "gk_" + "a" * 12 + "_" + "b" * 43})
        assert r.status_code == 503


class _Req:
    def __init__(self, peer: str, xff: str | None) -> None:
        self.client = type("C", (), {"host": peer})()
        self.headers = {"X-Forwarded-For": xff} if xff else {}


@pytest.mark.parametrize(
    ("source", "hops", "xff", "expected"),
    [
        ("peer", 1, "1.1.1.1", "10.0.0.5"),
        ("x-forwarded-for", 1, "6.6.6.6, 198.51.100.7", "198.51.100.7"),
        ("x-forwarded-for", 2, "6.6.6.6, 198.51.100.7, 172.16.0.1", "198.51.100.7"),
        ("x-forwarded-for", 2, "198.51.100.7", "10.0.0.5"),
        ("x-forwarded-for", 1, None, "10.0.0.5"),
    ],
)
def test_forwarded_for_is_only_trusted_as_configured(source, hops, xff, expected):
    settings = Settings(
        provider="geoapify", geoapify_api_key="k" * 20, client_ip_source=source, trusted_proxy_count=hops
    )
    assert client_ip(_Req("10.0.0.5", xff), settings) == expected  # type: ignore[arg-type]


def test_provider_budget_throttles_per_second_and_stops_at_the_daily_limit(redis_url):
    async def run() -> tuple[float, str]:
        client = aredis.Redis.from_url(redis_url, decode_responses=True)
        budget = ProviderBudget(client, "test", daily_limit=4, per_second=2)
        start = time.monotonic()
        for _ in range(3):  # the third call has to wait for the next second
            await budget.acquire(wait_seconds=2)
        waited = time.monotonic() - start
        await budget.acquire(wait_seconds=2)
        with pytest.raises(ApiError) as exhausted:
            await budget.acquire(wait_seconds=2)
        await client.aclose()
        return waited, exhausted.value.code

    redis.Redis.from_url(redis_url).flushdb()
    waited, code = asyncio.run(run())
    assert 0.0 < waited < 1.6
    assert code == "quota_exhausted"
