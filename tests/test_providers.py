"""Provider configuration and request/response mapping."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from tests.support import ADMIN_SECRET, PROVIDER_KEY, make_app

ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


def _api(redis_url, **settings):
    app, state = make_app(redis_url, **settings)
    client = TestClient(app)
    client.__enter__()
    key = client.post("/admin/keys", headers=ADMIN, json={"name": "p", "rate_per_minute": 500}).json()["api_key"]
    client.headers["X-API-Key"] = key
    return client, state


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        ({}, "PROVIDER must be set"),
        ({"provider": "geoapify"}, "GEOAPIFY_API_KEY"),
        ({"provider": "nominatim", "nominatim_user_agent": "x"}, "NOMINATIM_BASE_URL"),
        (
            {
                "provider": "nominatim",
                "nominatim_base_url": "https://nominatim.openstreetmap.org",
                "nominatim_user_agent": "x",
            },
            "public",
        ),
        ({"provider": "nominatim", "nominatim_base_url": "https://nominatim.example"}, "NOMINATIM_USER_AGENT"),
        ({"provider": "photon"}, "provider"),
    ],
)
def test_provider_configuration_errors(settings, message):
    with pytest.raises(ValueError, match=message):
        Settings(**settings)


def test_geoapify_request_mapping(redis_url):
    client, state = _api(redis_url)
    client.get(
        "/geocode",
        params={"q": " Berlin ", "countrycodes": "FR,de", "viewbox": "13,52,14,53", "lang": "DE", "limit": "3"},
    )
    call = state.calls[-1]
    assert call["path"] == "/v1/geocode/search"
    assert call["params"] == {
        "text": "Berlin",
        "format": "json",
        "limit": "3",
        "apiKey": PROVIDER_KEY,
        "filter": "countrycode:de,fr|rect:13.000000,52.000000,14.000000,53.000000",
        "lang": "de",
    }
    client.get("/reverse", params={"lat": "52.5", "lon": "13.4"})
    assert state.calls[-1]["path"] == "/v1/geocode/reverse"
    assert state.calls[-1]["params"]["lat"] == "52.500000" and state.calls[-1]["params"]["limit"] == "1"
    client.__exit__(None, None, None)


def test_geoapify_response_mapping_and_attribution(redis_url):
    client, _ = _api(redis_url)
    body = client.get("/geocode", params={"q": "Berlin"}).json()
    first = body["features"][0]
    assert first["id"].startswith("geoapify:fake")
    assert first["properties"]["label"].startswith("Berlin, 100 Main Street")
    assert first["properties"]["kind"] == "building"
    assert first["properties"]["address"]["city"] == "Exampleville"
    assert [a["text"] for a in body["attribution"]] == ["Powered by Geoapify", "© OpenStreetMap contributors"]
    client.__exit__(None, None, None)


def test_provider_key_never_reaches_the_caller(redis_url):
    client, _ = _api(redis_url)
    for response in (
        client.get("/geocode", params={"q": "Berlin"}),
        client.get("/reverse", params={"lat": "1", "lon": "2"}),
    ):
        assert PROVIDER_KEY not in response.text
        assert all(PROVIDER_KEY not in v for v in response.headers.values())
    client.__exit__(None, None, None)


def test_unexpected_provider_json_is_a_bad_response(redis_url, monkeypatch):
    client, _ = _api(redis_url)
    import tools.fake_upstream as fake

    original = fake._place
    monkeypatch.setattr(fake, "_place", lambda *a: {**original(*a), "lat": "not a number"})
    r = client.get("/geocode", params={"q": "Berlin"})
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_bad_response"
    client.__exit__(None, None, None)


def test_nominatim_adapter(redis_url):
    client, state = _api(
        redis_url,
        provider="nominatim",
        nominatim_base_url="http://fake-upstream",
        nominatim_user_agent="geocoding-api-tests/1.0 (tests@example.org)",
    )
    body = client.get("/geocode", params={"q": "Berlin", "viewbox": "13,52,14,53", "lang": "de"}).json()
    call = state.calls[-1]
    assert call["path"] == "/search"
    assert call["params"]["bounded"] == "1" and call["params"]["accept-language"] == "de"
    assert body["features"][0]["id"].startswith("nominatim:")
    assert "confidence" not in body["features"][0]["properties"]
    assert [a["text"] for a in body["attribution"]] == ["© OpenStreetMap contributors"]
    miss = client.get("/reverse", params={"lat": "85", "lon": "0"})
    assert miss.status_code == 200 and miss.json()["features"] == []
    client.__exit__(None, None, None)
