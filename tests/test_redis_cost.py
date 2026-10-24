"""How many Redis commands each kind of request costs.

Hosted Redis plans (Upstash) bill a script as one command plus one for every
call inside it. Redis's own INFO commandstats counts the same way, so these
numbers are what docs/OPERATIONS.md uses to estimate monthly usage. The test
fails if a change makes requests more expensive than documented.
"""

from __future__ import annotations

import redis
from fastapi.testclient import TestClient

from tests.support import ADMIN_SECRET, make_app

DOCUMENTED_MAXIMUM = {"demo_hit": 5, "demo_miss": 14, "key_hit": 7, "key_miss": 16, "rate_limited": 8}


def _commands(r: redis.Redis) -> int:
    stats = r.info("commandstats")
    return sum(
        v["calls"] for k, v in stats.items() if k not in ("cmdstat_info", "cmdstat_config|resetstat", "cmdstat_config")
    )


def measure(r: redis.Redis, fn) -> int:
    r.config_resetstat()
    fn()
    return _commands(r)


def test_redis_commands_per_request(redis_url):
    r = redis.Redis.from_url(redis_url)
    app, _ = make_app(redis_url, demo_rate_per_minute=3)
    costs = {}
    with TestClient(app) as client:
        client.get("/demo/geocode", params={"q": "warm scripts"})  # load scripts once
        costs["demo_miss"] = measure(r, lambda: client.get("/demo/geocode", params={"q": "Berlin"}))
        costs["demo_hit"] = measure(r, lambda: client.get("/demo/geocode", params={"q": "Berlin"}))
        costs["rate_limited"] = measure(r, lambda: client.get("/demo/geocode", params={"q": "Berlin"}))
        key = client.post("/admin/keys", headers={"X-Admin-Secret": ADMIN_SECRET}, json={"name": "c"}).json()["api_key"]
        h = {"X-API-Key": key}
        client.get("/geocode", params={"q": "warm"}, headers=h)
        costs["key_miss"] = measure(r, lambda: client.get("/geocode", params={"q": "Paris"}, headers=h))
        costs["key_hit"] = measure(r, lambda: client.get("/geocode", params={"q": "Paris"}, headers=h))
    print(costs)
    for name, maximum in DOCUMENTED_MAXIMUM.items():
        assert costs[name] <= maximum, (name, costs[name])
