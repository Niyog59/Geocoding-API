"""Regression tests for the findings in docs/FINDINGS.md.

Each test describes the corrected behaviour. Tests for findings that are not
fixed yet are marked `xfail(strict=True)`: they must fail until the fix lands,
and the commit that fixes a finding removes its marker.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.support import ADMIN_SECRET, make_app
from tools.fake_upstream import FakeState

ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


def pending(finding: str):
    return pytest.mark.xfail(strict=True, reason=f"{finding} not fixed yet")


@contextmanager
def client_for(redis_url: str, state: FakeState | None = None, **settings: Any) -> Iterator[tuple[TestClient, FakeState]]:
    app, state = make_app(redis_url, state, **settings)
    with TestClient(app) as client:
        yield client, state


def create_key(client: TestClient, **body: Any) -> tuple[str, str]:
    response = client.post("/admin/keys", headers=ADMIN, json={"name": "test", **body})
    assert response.status_code == 201, response.text
    data = response.json()
    return data["api_key"], data["id"]


def upstream_calls(state: FakeState) -> int:
    return len(state.calls)


# F1 -------------------------------------------------------------------------------------


@pending("F1")
def test_f1_builtin_demo_key_is_rejected(redis_url):
    with client_for(redis_url) as (client, _):
        response = client.get("/geocode", params={"q": "Berlin"}, headers={"X-API-Key": "geo_demo_key_for_testing_only"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_api_key"


# F2 -------------------------------------------------------------------------------------


@pending("F2")
def test_f2_credentials_in_query_string_are_rejected(redis_url):
    with client_for(redis_url) as (client, _):
        response = client.get("/geocode", params={"q": "Berlin", "api_key": "anything"})
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "credentials_in_url"


# F3 -------------------------------------------------------------------------------------


@pending("F3")
def test_f3_admin_uses_header_secret_and_rejects_ordinary_keys(redis_url):
    with client_for(redis_url) as (client, _):
        api_key, _ = create_key(client)
        assert client.post("/admin/keys", json={"name": "x"}).status_code == 401
        assert client.post("/admin/keys", headers={"X-API-Key": api_key}, json={"name": "x"}).status_code == 401
        assert client.post("/admin/keys", params={"admin_secret": ADMIN_SECRET}, json={"name": "x"}).status_code == 400


@pending("F3")
def test_f3_admin_secret_comparison_is_constant_time(redis_url, monkeypatch):
    calls = []
    original = hmac.compare_digest

    def spy(a, b):
        calls.append(True)
        return original(a, b)

    monkeypatch.setattr(hmac, "compare_digest", spy)
    with client_for(redis_url) as (client, _):
        client.get("/admin/keys", headers=ADMIN)
    assert calls


@pending("F3")
def test_f3_keys_are_hashed_shown_once_and_revocable(redis_url):
    import redis

    with client_for(redis_url) as (client, _):
        api_key, key_id = create_key(client)
        listing = client.get("/admin/keys", headers=ADMIN).json()
        assert api_key not in json.dumps(listing)
        stored = redis.Redis.from_url(redis_url)
        for name in stored.scan_iter("*"):
            value = stored.dump(name) or b""
            assert api_key.encode() not in value
        assert client.get("/geocode", params={"q": "Berlin"}, headers={"X-API-Key": api_key}).status_code == 200
        assert client.delete(f"/admin/keys/{key_id}", headers=ADMIN).status_code == 204
        revoked = client.get("/geocode", params={"q": "Paris"}, headers={"X-API-Key": api_key})
        assert revoked.status_code == 401


# F4 -------------------------------------------------------------------------------------


@pending("F4")
def test_f4_production_rejects_insecure_configuration(redis_url):
    with pytest.raises(Exception):
        with client_for(redis_url, app_env="production", admin_secret="short", cors_origins="*"):
            pass


# F5 -------------------------------------------------------------------------------------


@pending("F5")
def test_f5_keys_and_usage_survive_an_api_restart(redis_url):
    with client_for(redis_url) as (client, _):
        api_key, _ = create_key(client)
        assert client.get("/geocode", params={"q": "Berlin"}, headers={"X-API-Key": api_key}).status_code == 200
    with client_for(redis_url) as (client, _):
        headers = {"X-API-Key": api_key}
        assert client.get("/geocode", params={"q": "Berlin"}, headers=headers).status_code == 200
        usage = client.get("/usage", headers=headers).json()
        assert usage["today"]["total"] == 2


# F6 -------------------------------------------------------------------------------------


@pending("F6")
def test_f6_per_key_limits_apply_and_headers_match(redis_url):
    with client_for(redis_url) as (client, _):
        slow_key, _ = create_key(client, rate_per_minute=2)
        fast_key, _ = create_key(client, rate_per_minute=5)
        seen = []
        for _ in range(3):
            r = client.get("/geocode", params={"q": "Berlin"}, headers={"X-API-Key": slow_key})
            seen.append((r.status_code, r.headers.get("X-RateLimit-Limit"), r.headers.get("X-RateLimit-Remaining")))
        assert seen[:2] == [(200, "2", "1"), (200, "2", "0")]
        assert seen[2][0] == 429
        blocked = client.get("/geocode", params={"q": "Berlin"}, headers={"X-API-Key": slow_key})
        assert 1 <= int(blocked.headers["Retry-After"]) <= 60
        for _ in range(5):
            assert client.get("/geocode", params={"q": "Berlin"}, headers={"X-API-Key": fast_key}).status_code == 200


@pending("F6")
def test_f6_anonymous_demo_traffic_has_its_own_limit(redis_url):
    with client_for(redis_url, demo_rate_per_minute=3) as (client, _):
        codes = [client.get("/demo/geocode", params={"q": "Berlin"}).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]


@pending("F6")
def test_f6_concurrent_requests_cannot_exceed_a_key_limit(redis_url):
    app, _ = make_app(redis_url)

    async def run() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
                created = await client.post("/admin/keys", headers=ADMIN, json={"name": "c", "rate_per_minute": 5})
                key = created.json()["api_key"]
                responses = await asyncio.gather(
                    *[client.get("/geocode", params={"q": f"q{i}"}, headers={"X-API-Key": key}) for i in range(20)]
                )
                return [r.status_code for r in responses]

    codes = asyncio.run(run())
    assert codes.count(200) == 5
    assert codes.count(429) == 15


# F7 -------------------------------------------------------------------------------------


@pending("F7")
def test_f7_provider_budget_applies_to_every_caller(redis_url):
    with client_for(redis_url, provider_daily_budget=2) as (client, state):
        key, _ = create_key(client, rate_per_minute=100)
        codes = [client.get("/geocode", params={"q": f"place {i}"}, headers={"X-API-Key": key}).status_code for i in range(3)]
        assert codes == [200, 200, 503]
        assert upstream_calls(state) == 2


@pending("F7")
def test_f7_public_nominatim_is_never_a_default(redis_url):
    with pytest.raises(Exception):
        with client_for(redis_url, provider=""):
            pass
    with pytest.raises(Exception):
        with client_for(redis_url, provider="nominatim", nominatim_base_url="https://nominatim.openstreetmap.org"):
            pass


@pending("F7")
def test_f7_autocomplete_is_disabled(redis_url):
    with client_for(redis_url) as (client, _):
        response = client.get("/autocomplete", params={"q": "Ber"})
        assert response.status_code == 501
        assert response.json()["error"]["code"] == "autocomplete_disabled"


# F8 -------------------------------------------------------------------------------------


@pending("F8")
def test_f8_viewbox_is_part_of_the_cache_identity(redis_url):
    with client_for(redis_url) as (client, state):
        key, _ = create_key(client, rate_per_minute=100)
        headers = {"X-API-Key": key}
        a = client.get("/geocode", params={"q": "cafe", "viewbox": "10,50,11,51"}, headers=headers).json()
        b = client.get("/geocode", params={"q": "cafe", "viewbox": "-1,40,0,41"}, headers=headers).json()
        again = client.get("/geocode", params={"q": "cafe", "viewbox": "10,50,11,51"}, headers=headers).json()
        assert upstream_calls(state) == 2
        assert a["features"][0]["geometry"] != b["features"][0]["geometry"]
        assert again["features"] == a["features"]


# F9 -------------------------------------------------------------------------------------


@pending("F9")
def test_f9_identical_concurrent_requests_share_one_upstream_call(redis_url):
    state = FakeState()
    state.delay_seconds = 0.2
    app, _ = make_app(redis_url, state)

    async def run() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://api") as client:
                created = await client.post("/admin/keys", headers=ADMIN, json={"name": "c", "rate_per_minute": 100})
                key = created.json()["api_key"]
                responses = await asyncio.gather(
                    *[client.get("/geocode", params={"q": "Same Place"}, headers={"X-API-Key": key}) for _ in range(10)]
                )
                return [r.status_code for r in responses]

    assert asyncio.run(run()) == [200] * 10
    assert upstream_calls(state) == 1


# F10 ------------------------------------------------------------------------------------


@pending("F10")
def test_f10_label_falls_back_to_address_parts(redis_url):
    with client_for(redis_url) as (client, _):
        key, _ = create_key(client)
        body = client.get("/geocode", params={"q": "unlabeled spot"}, headers={"X-API-Key": key}).json()
        labels = [f["properties"]["label"] for f in body["features"]]
        assert labels and all(label and label != "Unknown" for label in labels)
        assert "Main Street" in labels[0]


# F11 ------------------------------------------------------------------------------------


FEATURE_ID_SCRIPT = """
import json, sys
from fastapi.testclient import TestClient
from tests.support import ADMIN_SECRET, make_app
app, _ = make_app(sys.argv[1])
with TestClient(app) as c:
    key = c.post("/admin/keys", headers={"X-Admin-Secret": ADMIN_SECRET}, json={"name": "id"}).json()["api_key"]
    body = c.get("/geocode", params={"q": "unlabeled spot"}, headers={"X-API-Key": key}).json()
    print(json.dumps([f["id"] for f in body["features"]]))
"""


@pending("F11")
def test_f11_feature_ids_are_stable_across_processes(redis_url):
    import redis

    ids = []
    for seed in ("1", "2"):
        redis.Redis.from_url(redis_url).flushdb()
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run(
            [sys.executable, "-c", FEATURE_ID_SCRIPT, redis_url], capture_output=True, text=True, env=env, check=True
        )
        ids.append(json.loads(out.stdout.strip().splitlines()[-1]))
    assert ids[0] == ids[1]
    assert len(set(ids[0])) == len(ids[0])


# F12 ------------------------------------------------------------------------------------


@pending("F12")
def test_f12_no_invented_relevance_scores(redis_url):
    with client_for(redis_url) as (client, _):
        key, _ = create_key(client)
        body = client.get("/geocode", params={"q": "Berlin"}, headers={"X-API-Key": key}).json()
        first = body["features"][0]
        assert "relevance" not in first
        assert first["properties"].get("confidence") == 0.9


# F13 ------------------------------------------------------------------------------------


LOGGING_SCRIPT = """
import logging, sys
from tests.support import make_app
app, _ = make_app(sys.argv[1])
logging.getLogger("httpx").warning("HTTP Request: GET https://api.example/v1?text=x&apiKey=SECRET123")
logging.getLogger("uvicorn.error").warning("third-party message without a request id")
"""


@pending("F13")
def test_f13_third_party_logs_format_cleanly_and_redact_credentials(redis_url):
    out = subprocess.run([sys.executable, "-c", LOGGING_SCRIPT, redis_url], capture_output=True, text=True, check=True)
    combined = out.stdout + out.stderr
    assert "Logging error" not in combined
    assert "third-party message without a request id" in combined
    assert "SECRET123" not in combined


# F14 ------------------------------------------------------------------------------------


@pending("F14")
@pytest.mark.parametrize(
    ("mode", "status", "expected_status", "expected_code"),
    [
        ("status", 500, 502, "upstream_error"),
        ("status", 429, 503, "upstream_rate_limited"),
        ("malformed", 200, 502, "upstream_bad_response"),
    ],
)
def test_f14_upstream_failures_return_structured_errors(redis_url, mode, status, expected_status, expected_code):
    state = FakeState()
    state.mode = mode
    state.status_code = status
    with client_for(redis_url, state, upstream_max_retries=0) as (client, _):
        key, _ = create_key(client)
        response = client.get("/geocode", params={"q": "Berlin"}, headers={"X-API-Key": key})
        assert response.status_code == expected_status
        error = response.json()["error"]
        assert error["code"] == expected_code
        assert error["request_id"] == response.headers["X-Request-ID"]
        assert "X-Response-Time" in response.headers


# F15 ------------------------------------------------------------------------------------


@pending("F15")
def test_f15_health_endpoints_do_not_expose_inventory_or_spend_credits(redis_url):
    with client_for(redis_url) as (client, state):
        root = client.get("/").json()
        assert "api_keys_loaded" not in root and "cache_entries" not in root
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert upstream_calls(state) == 0
