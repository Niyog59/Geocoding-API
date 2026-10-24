"""Input validation and response shape."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.support import ADMIN_SECRET, make_app

ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


@pytest.fixture
def api(redis_url):
    app, state = make_app(redis_url)
    with TestClient(app) as client:
        key = client.post("/admin/keys", headers=ADMIN, json={"name": "v", "rate_per_minute": 600}).json()["api_key"]
        client.headers["X-API-Key"] = key
        yield client, state


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"q": ""},
        {"q": "   "},
        {"q": "a" * 201},
        {"q": "Berlin\x00"},
        {"q": "Berlin", "limit": "0"},
        {"q": "Berlin", "limit": "11"},
        {"q": "Berlin", "limit": "two"},
        {"q": "Berlin", "countrycodes": "usa"},
        {"q": "Berlin", "countrycodes": ","},
        {"q": "Berlin", "countrycodes": ",".join(f"a{c}" for c in "bcdefghijkl")},
        {"q": "Berlin", "viewbox": "1,2,3"},
        {"q": "Berlin", "viewbox": "14,53,13,52"},
        {"q": "Berlin", "viewbox": "a,b,c,d"},
        {"q": "Berlin", "viewbox": "-200,0,10,10"},
        {"q": "Berlin", "lang": "english"},
        {"q": "Berlin", "bounded": "1"},
        {"q": "Berlin", "unknown": "x"},
    ],
)
def test_invalid_search_input_is_rejected(api, params):
    client, state = api
    r = client.get("/geocode", params=params)
    assert r.status_code == 400, params
    assert r.json()["error"]["code"] == "invalid_request"
    assert state.calls == []


def test_query_of_exactly_200_characters_is_accepted(api):
    client, _ = api
    assert client.get("/geocode", params={"q": "a" * 200}).status_code == 200


def test_repeated_parameters_are_rejected(api):
    client, _ = api
    r = client.get("/geocode?q=Berlin&q=Paris")
    assert r.status_code == 400


@pytest.mark.parametrize(
    "params",
    [
        {"lat": "nan", "lon": "0"},
        {"lat": "inf", "lon": "0"},
        {"lat": "91", "lon": "0"},
        {"lat": "0", "lon": "180.1"},
        {"lat": "0"},
        {"lat": "", "lon": "0"},
        {"lat": "1e400", "lon": "0"},
        {"lat": "10", "lon": "20", "zoom": "18"},
    ],
)
def test_invalid_reverse_input_is_rejected(api, params):
    client, _ = api
    r = client.get("/reverse", params=params)
    assert r.status_code == 400, params


def test_zoom_gets_an_explanation(api):
    client, _ = api
    message = client.get("/reverse", params={"lat": "1", "lon": "2", "zoom": "5"}).json()["error"]["message"]
    assert "zoom is no longer supported" in message


@pytest.mark.parametrize(("lat", "lon"), [("-90", "-180"), ("90", "180"), ("0", "0")])
def test_coordinate_boundaries_are_accepted(api, lat, lon):
    client, _ = api
    assert client.get("/reverse", params={"lat": lat, "lon": lon}).status_code == 200


def test_geojson_uses_longitude_then_latitude(api):
    client, _ = api
    body = client.get("/reverse", params={"lat": "10.5", "lon": "-20.25"}).json()
    assert body["features"][0]["geometry"]["coordinates"] == [-20.25, 10.5]


def test_reverse_miss_returns_an_empty_collection(api):
    client, _ = api
    r = client.get("/reverse", params={"lat": "85", "lon": "0"})
    assert r.status_code == 200
    assert r.json()["features"] == []


def test_search_with_no_matches_returns_an_empty_collection(api):
    client, _ = api
    r = client.get("/geocode", params={"q": "nowhere special"})
    assert r.status_code == 200
    assert r.json()["type"] == "FeatureCollection" and r.json()["features"] == []


def test_viewbox_is_sent_upstream_as_a_hard_filter(api):
    client, state = api
    client.get("/geocode", params={"q": "cafe", "viewbox": "13,52,14,53"})
    sent = state.calls[-1]["params"]
    assert sent["filter"] == "rect:13.000000,52.000000,14.000000,53.000000"


def test_demo_rejects_limits_above_its_maximum(redis_url):
    app, _ = make_app(redis_url, demo_max_results=5)
    with TestClient(app) as client:
        r = client.get("/demo/geocode", params={"q": "Berlin", "limit": "6"})
        assert r.status_code == 400
        assert "between 1 and 5" in r.json()["error"]["message"]
