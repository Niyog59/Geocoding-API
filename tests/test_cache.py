"""Shared cache behaviour."""

from __future__ import annotations

import asyncio
import time

import httpx
import redis
from fastapi.testclient import TestClient

from tests.support import ADMIN_SECRET, make_app
from tools.fake_upstream import FakeState

ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


def _client(redis_url, state=None, **settings):
    app, state = make_app(redis_url, state, **settings)
    return TestClient(app), state


def _headers(client):
    key = client.post("/admin/keys", headers=ADMIN, json={"name": "c", "rate_per_minute": 500}).json()["api_key"]
    return {"X-API-Key": key}


def test_entries_expire(redis_url):
    client, state = _client(redis_url, cache_ttl_seconds=1)
    with client:
        h = _headers(client)
        assert client.get("/geocode", params={"q": "Berlin"}, headers=h).headers["X-Cache"] == "MISS"
        assert client.get("/geocode", params={"q": "Berlin"}, headers=h).headers["X-Cache"] == "HIT"
        time.sleep(1.2)
        assert client.get("/geocode", params={"q": "Berlin"}, headers=h).headers["X-Cache"] == "MISS"
        assert len(state.calls) == 2


def test_empty_results_are_cached_for_the_shorter_empty_ttl(redis_url):
    client, state = _client(redis_url, cache_ttl_seconds=3600, empty_cache_ttl_seconds=60)
    with client:
        h = _headers(client)
        first = client.get("/geocode", params={"q": "nowhere at all"}, headers=h)
        second = client.get("/geocode", params={"q": "nowhere at all"}, headers=h)
        assert first.status_code == 200 and first.json()["features"] == []
        assert second.headers["X-Cache"] == "HIT"
        assert len(state.calls) == 1
        r = redis.Redis.from_url(redis_url)
        ttls = [r.pttl(k) for k in r.scan_iter("geo:cache:*") if k != b"geo:cache:index"]
        assert ttls and all(0 < t <= 60_000 for t in ttls)


def test_failures_are_not_cached(redis_url):
    state = FakeState()
    state.mode, state.status_code, state.remaining_mode_requests = "status", 500, 1
    client, state = _client(redis_url, state)
    with client:
        h = _headers(client)
        assert client.get("/geocode", params={"q": "Berlin"}, headers=h).status_code >= 500
        ok = client.get("/geocode", params={"q": "Berlin"}, headers=h)
        assert ok.status_code == 200 and ok.headers["X-Cache"] == "MISS"


def test_storage_is_bounded(redis_url):
    client, _ = _client(redis_url, cache_max_entries=3)
    with client:
        h = _headers(client)
        for i in range(6):
            client.get("/geocode", params={"q": f"place {i}"}, headers=h)
        r = redis.Redis.from_url(redis_url)
        entries = [k for k in r.scan_iter("geo:cache:*") if k != b"geo:cache:index"]
        assert r.zcard("geo:cache:index") == 3
        assert len(entries) == 3


def test_equivalent_inputs_share_an_entry_and_different_inputs_do_not(redis_url):
    client, state = _client(redis_url)
    with client:
        h = _headers(client)
        same = [
            {"q": "Berlin"},
            {"q": "  berlin  "},
            {"q": "BERLIN"},
        ]
        for params in same:
            client.get("/geocode", params=params, headers=h)
        assert len(state.calls) == 1
        client.get("/geocode", params={"q": "Berlin", "countrycodes": "de,fr"}, headers=h)
        client.get("/geocode", params={"q": "Berlin", "countrycodes": "FR, de"}, headers=h)
        assert len(state.calls) == 2
        for params in (
            {"q": "Berlin", "countrycodes": "de"},
            {"q": "Berlin", "lang": "de"},
            {"q": "Berlin", "limit": "2"},
            {"q": "Berlin", "viewbox": "13,52,14,53"},
        ):
            client.get("/geocode", params=params, headers=h)
        assert len(state.calls) == 6


def test_concurrent_misses_in_two_separate_app_instances_share_one_upstream_call(redis_url):
    """Two app instances stand in for two workers: they share only Redis."""
    state = FakeState()
    state.delay_seconds = 0.3
    app_a, _ = make_app(redis_url, state)
    app_b, _ = make_app(redis_url, state)

    async def run() -> list[str]:
        async with app_a.router.lifespan_context(app_a), app_b.router.lifespan_context(app_b):
            a = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_a), base_url="http://a")
            b = httpx.AsyncClient(transport=httpx.ASGITransport(app=app_b), base_url="http://b")
            created = await a.post("/admin/keys", headers=ADMIN, json={"name": "c", "rate_per_minute": 500})
            h = {"X-API-Key": created.json()["api_key"]}
            requests = [c.get("/geocode", params={"q": "Same Place"}, headers=h) for c in (a, b, a, b)]
            responses = await asyncio.gather(*requests)
            await a.aclose()
            await b.aclose()
            assert {r.status_code for r in responses} == {200}
            assert len({str(r.json()["features"]) for r in responses}) == 1
            return [r.headers["X-Cache"] for r in responses]

    statuses = asyncio.run(run())
    assert len(state.calls) == 1
    assert statuses.count("MISS") == 1 and statuses.count("SHARED") == 3
