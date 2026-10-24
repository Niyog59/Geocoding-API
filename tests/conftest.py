"""Shared fixtures.

Tests run against a real Redis server, never a fake. If REDIS_URL is set (as in
CI, where Redis runs as a service container) that server is used; otherwise a
throwaway `redis-server` is started for the test session.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator

import pytest
import redis


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    configured = os.getenv("TEST_REDIS_URL")
    if configured:
        yield configured
        return
    binary = shutil.which("redis-server")
    if not binary:
        pytest.exit("redis-server not found. Install Redis or set TEST_REDIS_URL.", returncode=2)
    port = _free_port()
    workdir = tempfile.mkdtemp(prefix="geo-test-redis-")
    proc = subprocess.Popen(
        [binary, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no", "--dir", workdir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"redis://127.0.0.1:{port}/0"
    client = redis.Redis.from_url(url)
    for _ in range(100):
        try:
            client.ping()
            break
        except redis.ConnectionError:
            time.sleep(0.05)
    yield url
    proc.terminate()
    proc.wait(timeout=10)
    shutil.rmtree(workdir, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_redis(redis_url: str) -> Iterator[None]:
    redis.Redis.from_url(redis_url).flushdb()
    yield
