"""Upstream failures, deadlines, logging, health, metrics and CORS."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.support import ADMIN_SECRET, make_app
from tools.fake_upstream import FakeState

ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


def _state(**kw) -> FakeState:
    state = FakeState()
    for k, v in kw.items():
        setattr(state, k, v)
    return state


def _client_and_key(redis_url, state=None, **settings):
    app, state = make_app(redis_url, state, **settings)
    client = TestClient(app)
    client.__enter__()
    key = client.post("/admin/keys", headers=ADMIN, json={"name": "r", "rate_per_minute": 500}).json()["api_key"]
    client.headers["X-API-Key"] = key
    return client, state


def test_timeout_returns_504(redis_url):
    client, _ = _client_and_key(
        redis_url, _state(mode="hang"), upstream_connect_timeout=0.2, upstream_read_timeout=0.3, upstream_max_retries=0
    )
    start = time.monotonic()
    r = client.get("/geocode", params={"q": "Berlin"})
    assert r.status_code == 504 and r.json()["error"]["code"] == "upstream_timeout"
    assert time.monotonic() - start < 2
    client.__exit__(None, None, None)


def test_request_deadline_caps_total_time(redis_url):
    client, _ = _client_and_key(redis_url, _state(delay_seconds=3), request_deadline_seconds=0.5)
    start = time.monotonic()
    r = client.get("/geocode", params={"q": "Berlin"})
    assert r.status_code == 504
    assert time.monotonic() - start < 1.5
    client.__exit__(None, None, None)


def test_transient_failure_is_retried_once(redis_url):
    client, state = _client_and_key(redis_url, _state(mode="status", status_code=503, remaining_mode_requests=1))
    r = client.get("/geocode", params={"q": "Berlin"})
    assert r.status_code == 200
    assert len(state.calls) == 2
    client.__exit__(None, None, None)


def test_retries_are_bounded(redis_url):
    client, state = _client_and_key(redis_url, _state(mode="status", status_code=503), upstream_max_retries=1)
    r = client.get("/geocode", params={"q": "Berlin"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_error"
    assert len(state.calls) == 2
    client.__exit__(None, None, None)


def test_short_retry_after_is_honoured(redis_url):
    state = _state(mode="status", status_code=429, retry_after="1", remaining_mode_requests=1)
    client, state = _client_and_key(redis_url, state)
    start = time.monotonic()
    r = client.get("/geocode", params={"q": "Berlin"})
    assert r.status_code == 200
    assert time.monotonic() - start >= 0.95
    assert len(state.calls) == 2
    client.__exit__(None, None, None)


def test_long_retry_after_is_passed_back_instead_of_waiting(redis_url):
    client, state = _client_and_key(redis_url, _state(mode="status", status_code=429, retry_after="30"))
    r = client.get("/geocode", params={"q": "Berlin"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "upstream_rate_limited"
    assert r.headers["Retry-After"] == "30"
    assert len(state.calls) == 1
    client.__exit__(None, None, None)


def test_retries_count_against_the_provider_budget(redis_url):
    state = _state(mode="status", status_code=503, remaining_mode_requests=1)
    client, state = _client_and_key(redis_url, state, provider_daily_budget=2)
    assert client.get("/geocode", params={"q": "first"}).status_code == 200
    r = client.get("/geocode", params={"q": "second"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "quota_exhausted"
    assert len(state.calls) == 2
    client.__exit__(None, None, None)


def test_rejected_provider_credentials_are_not_retried(redis_url):
    client, state = _client_and_key(redis_url, _state(mode="status", status_code=401))
    r = client.get("/geocode", params={"q": "Berlin"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "upstream_unavailable"
    assert len(state.calls) == 1
    client.__exit__(None, None, None)


def test_upstream_concurrency_is_bounded(redis_url):
    app, _ = make_app(redis_url, _state(delay_seconds=0.6), upstream_concurrency=1, upstream_queue_seconds=0.1)

    async def run() -> list[str]:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api") as c:
                created = await c.post("/admin/keys", headers=ADMIN, json={"name": "q", "rate_per_minute": 500})
                h = {"X-API-Key": created.json()["api_key"]}
                rs = await asyncio.gather(*[c.get("/geocode", params={"q": f"p{i}"}, headers=h) for i in range(2)])
                return sorted(r.json().get("error", {}).get("code", "ok") for r in rs)

    assert asyncio.run(run()) == ["ok", "upstream_busy"]


def test_unexpected_errors_are_structured(redis_url, monkeypatch):
    client, _ = _client_and_key(redis_url)

    async def boom(_params):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(client.app.state.service, "search", boom)
    r = client.get("/geocode", params={"q": "Berlin"})
    assert r.status_code == 500
    body = r.json()["error"]
    assert body["code"] == "internal_error" and "secret internal detail" not in r.text
    assert body["request_id"] == r.headers["X-Request-ID"]
    client.__exit__(None, None, None)


def test_request_logs_are_json_with_request_id_and_no_query_or_secrets(redis_url, capsys):
    client, _ = _client_and_key(redis_url)
    r = client.get("/geocode", params={"q": "221B Baker Street"})
    logging.getLogger("app.test").warning("admin secret is %s", ADMIN_SECRET)
    err = capsys.readouterr().err
    lines = [json.loads(line) for line in err.splitlines() if line.startswith("{")]
    request_lines = [line for line in lines if line["message"] == "request" and line.get("route") == "/geocode"]
    assert request_lines and request_lines[-1]["request_id"] == r.headers["X-Request-ID"]
    assert "Baker" not in err
    assert ADMIN_SECRET not in err and client.headers["X-API-Key"] not in err
    client.__exit__(None, None, None)


def test_readiness_reports_redis_outage(redis_url):
    app, _ = make_app("redis://127.0.0.1:1/0")
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        assert ready.status_code == 503 and ready.json()["checks"]["redis"] == "unavailable"


def test_metrics_require_admin_and_use_bounded_labels(redis_url):
    client, _ = _client_and_key(redis_url)
    client.get("/geocode", params={"q": "Unique Street 123"})
    assert client.get("/metrics").status_code == 401
    body = client.get("/metrics", headers=ADMIN).text
    assert 'geo_http_requests_total{method="GET",route="/geocode",status_class="2xx"}' in body
    assert "Unique Street" not in body and "req_" not in body
    client.__exit__(None, None, None)


@pytest.mark.parametrize(("origin", "allowed"), [("https://play.example", True), ("https://evil.example", False)])
def test_cors_allows_only_configured_origins(redis_url, origin, allowed):
    app, _ = make_app(redis_url, cors_origins="https://play.example")
    with TestClient(app) as client:
        r = client.get("/demo/geocode", params={"q": "Berlin"}, headers={"Origin": origin})
        assert (r.headers.get("access-control-allow-origin") == origin) is allowed
        assert "access-control-allow-credentials" not in r.headers
        if allowed:
            exposed = r.headers["access-control-expose-headers"]
            assert "X-RateLimit-Remaining" in exposed and "Retry-After" in exposed
            preflight = client.options(
                "/geocode",
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": "GET",
                    "Access-Control-Request-Headers": "X-API-Key",
                },
            )
            assert preflight.status_code == 200
