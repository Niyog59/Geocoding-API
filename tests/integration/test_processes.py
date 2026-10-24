"""Integration tests with real processes.

These start the fake provider and several API processes with uvicorn (one of
them with two workers), all talking to a real Redis server over TCP. They
check that state really is shared between processes and survives restarts,
which in-process tests cannot show.

Run with:  pytest -m integration
"""

from __future__ import annotations

import concurrent.futures
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import redis

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
ADMIN_SECRET = "integration-admin-secret-" + "z" * 32
ADMIN = {"X-Admin-Secret": ADMIN_SECRET}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(url: str, timeout: float = 20) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{url} did not become ready")


class Process:
    def __init__(self, args: list[str], env: dict[str, str], ready_url: str) -> None:
        self.args, self.env, self.ready_url = args, env, ready_url
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> Process:
        self.proc = subprocess.Popen(
            self.args, cwd=ROOT, env={**os.environ, **self.env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        wait_for(self.ready_url)
        return self

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()


@pytest.fixture(scope="module")
def fake_provider() -> Iterator[str]:
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    proc = Process(
        [sys.executable, "-m", "uvicorn", "tools.fake_upstream:app", "--port", str(port), "--log-level", "warning"],
        {},
        f"{url}/__control/state",
    ).start()
    yield url
    proc.stop()


def api(redis_url: str, provider_url: str, workers: int = 1, **settings: str) -> Process:
    port = free_port()
    env = {
        "APP_ENV": "test",
        "PROVIDER": "geoapify",
        "GEOAPIFY_API_KEY": "integration-provider-key",
        "GEOAPIFY_BASE_URL": provider_url,
        "REDIS_URL": redis_url,
        "ADMIN_SECRET": ADMIN_SECRET,
        "PROVIDER_RATE_PER_SECOND": "1000",
        "LOG_LEVEL": "WARNING",
        **{k.upper(): v for k, v in settings.items()},
    }
    args = [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(port)]
    args += ["--workers", str(workers), "--no-access-log", "--log-level", "warning"]
    proc = Process(args, env, f"http://127.0.0.1:{port}/health/ready")
    proc.base = f"http://127.0.0.1:{port}"  # type: ignore[attr-defined]
    return proc


@contextmanager
def running(*procs: Process) -> Iterator[list[str]]:
    started = [p.start() for p in procs]
    try:
        yield [p.base for p in started]  # type: ignore[attr-defined]
    finally:
        for p in started:
            p.stop()


def provider_calls(provider_url: str) -> int:
    return httpx.get(f"{provider_url}/__control/state").json()["calls"]


@pytest.fixture(autouse=True)
def reset_provider(fake_provider: str) -> None:
    httpx.post(f"{fake_provider}/__control/reset")


def create_key(base: str, **body: object) -> str:
    r = httpx.post(f"{base}/admin/keys", headers=ADMIN, json={"name": "it", **body})
    assert r.status_code == 201, r.text
    return r.json()["api_key"]


def test_keys_and_revocation_are_shared_between_processes(redis_url, fake_provider):
    with running(api(redis_url, fake_provider, workers=2), api(redis_url, fake_provider)) as (a, b):
        key = create_key(a)
        headers = {"X-API-Key": key}
        for _ in range(4):  # several requests so both workers of `a` are likely to serve one
            assert httpx.get(f"{a}/geocode", params={"q": "Berlin"}, headers=headers).status_code == 200
        assert httpx.get(f"{b}/geocode", params={"q": "Berlin"}, headers=headers).status_code == 200
        key_id = key.split("_")[1]
        assert httpx.delete(f"{b}/admin/keys/{key_id}", headers=ADMIN).status_code == 204
        for base in (a, a, b):
            assert httpx.get(f"{base}/geocode", params={"q": "Berlin"}, headers=headers).status_code == 401


def test_rate_limit_holds_across_workers_and_processes_under_concurrency(redis_url, fake_provider):
    with running(api(redis_url, fake_provider, workers=2), api(redis_url, fake_provider)) as (a, b):
        key = create_key(a, rate_per_minute=10)
        headers = {"X-API-Key": key}

        def call(i: int) -> int:
            base = a if i % 2 else b
            return httpx.get(
                f"{base}/geocode", params={"q": f"concurrent {i}"}, headers=headers, timeout=10
            ).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
            codes = list(pool.map(call, range(40)))
        assert codes.count(200) == 10
        assert codes.count(429) == 30
        usage = httpx.get(f"{b}/usage", headers=headers).json()
        assert usage["today"]["total"] == 10


def test_cache_and_coalescing_are_shared_between_processes(redis_url, fake_provider):
    with running(api(redis_url, fake_provider), api(redis_url, fake_provider)) as (a, b):
        key = create_key(a, rate_per_minute=100)
        headers = {"X-API-Key": key}
        first = httpx.get(f"{a}/geocode", params={"q": "Shared Cache"}, headers=headers)
        second = httpx.get(f"{b}/geocode", params={"q": "Shared Cache"}, headers=headers)
        assert (first.headers["X-Cache"], second.headers["X-Cache"]) == ("MISS", "HIT")
        assert first.json()["features"] == second.json()["features"]
        assert provider_calls(fake_provider) == 1

        httpx.post(f"{fake_provider}/__control/configure", json={"delay_seconds": 0.5})

        def call(base: str) -> str:
            return httpx.get(f"{base}/geocode", params={"q": "Coalesced"}, headers=headers, timeout=10).headers[
                "X-Cache"
            ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(call, [a, b, a, b, a, b]))
        assert provider_calls(fake_provider) == 2
        assert outcomes.count("MISS") == 1


def test_provider_budget_is_shared_between_processes(redis_url, fake_provider):
    procs = (
        api(redis_url, fake_provider, provider_daily_budget="3"),
        api(redis_url, fake_provider, provider_daily_budget="3"),
    )
    with running(*procs) as (a, b):
        key = create_key(a, rate_per_minute=100)
        headers = {"X-API-Key": key}
        codes = [
            httpx.get(f"{base}/geocode", params={"q": f"budget {i}"}, headers=headers).status_code
            for i, base in enumerate([a, b, a, b, a])
        ]
        assert codes == [200, 200, 200, 503, 503]
        assert provider_calls(fake_provider) == 3


def test_keys_usage_and_cache_survive_an_api_restart(redis_url, fake_provider):
    with running(api(redis_url, fake_provider)) as (a,):
        key = create_key(a)
        headers = {"X-API-Key": key}
        assert httpx.get(f"{a}/geocode", params={"q": "Before Restart"}, headers=headers).headers["X-Cache"] == "MISS"
    with running(api(redis_url, fake_provider)) as (a,):
        r = httpx.get(f"{a}/geocode", params={"q": "Before Restart"}, headers=headers)
        assert r.status_code == 200 and r.headers["X-Cache"] == "HIT"
        assert httpx.get(f"{a}/usage", headers=headers).json()["today"]["total"] == 2
    assert provider_calls(fake_provider) == 1


@contextmanager
def redis_server(directory: str, port: int, *extra: str) -> Iterator[subprocess.Popen[bytes]]:
    binary = shutil.which("redis-server")
    if not binary:
        pytest.skip("redis-server is needed for this test")
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1", "--dir", directory, *extra],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    client = redis.Redis(port=port)
    for _ in range(100):
        try:
            client.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.05)
    try:
        yield proc
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_configured_redis_persistence_keeps_keys_across_a_redis_restart(fake_provider):
    """Uses the same persistence settings as docker-compose.yml (append-only file, fsync every second)."""
    directory = tempfile.mkdtemp(prefix="geo-persist-")
    port = free_port()
    url = f"redis://127.0.0.1:{port}/0"
    persistence = ("--appendonly", "yes", "--appendfsync", "everysec")
    try:
        with redis_server(directory, port, *persistence):
            with running(api(url, fake_provider)) as (a,):
                key = create_key(a)
            redis.Redis(port=port).shutdown(save=True)
        with redis_server(directory, port, *persistence), running(api(url, fake_provider)) as (a,):
            assert httpx.get(f"{a}/geocode", params={"q": "Persisted"}, headers={"X-API-Key": key}).status_code == 200
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_requests_fail_closed_while_redis_is_down_and_recover_after(fake_provider):
    directory = tempfile.mkdtemp(prefix="geo-outage-")
    port = free_port()
    url = f"redis://127.0.0.1:{port}/0"
    try:
        with redis_server(directory, port, "--appendonly", "yes") as server:
            proc = api(url, fake_provider).start()
            base = proc.base  # type: ignore[attr-defined]
            key = create_key(base)
            headers = {"X-API-Key": key}
            assert httpx.get(f"{base}/geocode", params={"q": "Up"}, headers=headers).status_code == 200
            redis.Redis(port=port).shutdown(save=True)
            server.wait(timeout=10)
            down = [
                httpx.get(f"{base}/geocode", params={"q": f"Down {i}"}, headers=headers, timeout=10) for i in range(3)
            ]
            assert {r.status_code for r in down} == {503}
            assert httpx.get(f"{base}/demo/geocode", params={"q": "Down"}, timeout=10).status_code == 503
            assert httpx.get(f"{base}/health/ready", timeout=10).status_code == 503
            assert httpx.get(f"{base}/health/live", timeout=10).status_code == 200
        with redis_server(directory, port, "--appendonly", "yes"):
            assert httpx.get(f"{base}/geocode", params={"q": "Back"}, headers=headers, timeout=10).status_code == 200
        proc.stop()
    finally:
        shutil.rmtree(directory, ignore_errors=True)
