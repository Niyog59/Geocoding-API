"""Reproducible local benchmark.

Starts an isolated stack (a throwaway Redis, the fake provider with a fixed
100 ms delay, and the API), then runs:

1. warm-up;
2. cache-hit workload: a fixed set of already-cached queries;
3. cache-miss workload: a new query for every request, so each one calls the
   (fake) provider;
4. quota scenario: a small provider budget, checked to never be exceeded;
5. concurrency scenario: bursts against one key's limit and a revoked key.

Nothing here contacts a real geocoding provider.

    python -m bench.run_benchmark --duration 300 --rate 10
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import httpx

from bench.loadgen import run

ROOT = Path(__file__).resolve().parents[1]
ADMIN_SECRET = "benchmark-admin-secret-" + "b" * 32
ADMIN = {"X-Admin-Secret": ADMIN_SECRET}
UPSTREAM_DELAY_MS = 100


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_for(url: str, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"{url} not ready")


def start(args: list[str], env: dict[str, str], ready: str | None = None) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        args, cwd=ROOT, env={**os.environ, **env}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    if ready:
        wait_for(ready)
    return proc


def stop(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


def process_tree(pid: int) -> list[int]:
    out = subprocess.run(["ps", "-o", "pid=", "--ppid", str(pid)], capture_output=True, text=True).stdout.split()
    pids = [pid]
    for child in out:
        pids += process_tree(int(child))
    return pids


def cpu_seconds_and_rss_mb(pid: int) -> tuple[float, float]:
    ticks = os.sysconf("SC_CLK_TCK")
    cpu, rss = 0.0, 0.0
    for p in process_tree(pid):
        try:
            fields = Path(f"/proc/{p}/stat").read_text().rsplit(")", 1)[1].split()
            cpu += (int(fields[11]) + int(fields[12])) / ticks
            for line in Path(f"/proc/{p}/status").read_text().splitlines():
                if line.startswith("VmRSS:"):
                    rss += int(line.split()[1]) / 1024
        except (FileNotFoundError, ProcessLookupError):
            pass
    return cpu, rss


def environment(workers: int) -> dict[str, object]:
    cpu_model = next(
        (
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        ),
        platform.processor(),
    )
    mem_kb = next(
        int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemTotal")
    )
    redis_version = subprocess.run(["redis-server", "--version"], capture_output=True, text=True).stdout.split()[2]
    return {
        "git_commit": subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True
        ).stdout.strip(),
        "date_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "os": platform.platform(),
        "cpu": f"{cpu_model} ({os.cpu_count()} logical CPUs)",
        "memory_gb": round(mem_kb / 1024 / 1024, 1),
        "python": platform.python_version(),
        "redis_server": redis_version.removeprefix("v="),
        "packages": {p: version(p) for p in ("fastapi", "uvicorn", "httpx", "redis")},
        "api_workers": workers,
        "redis_location": "local, same machine, TCP on 127.0.0.1, persistence off",
        "provider": f"local fake provider (tools/fake_upstream.py) with a fixed {UPSTREAM_DELAY_MS} ms delay",
        "load_generator": "bench/loadgen.py: httpx.AsyncClient, fixed arrival rate (open model), same machine",
    }


def api_env(redis_url: str, provider_url: str, **overrides: str) -> dict[str, str]:
    env = {
        "APP_ENV": "test",
        "PROVIDER": "geoapify",
        "GEOAPIFY_API_KEY": "benchmark-fake-key",
        "GEOAPIFY_BASE_URL": provider_url,
        "REDIS_URL": redis_url,
        "ADMIN_SECRET": ADMIN_SECRET,
        "LOG_LEVEL": "WARNING",
        # Benchmark-only quotas, high enough that the scenarios measure speed, not limits.
        "MAX_KEY_RATE_PER_MINUTE": "100000",
        "MAX_KEY_DAILY_LIMIT": "1000000",
        "PROVIDER_DAILY_BUDGET": "1000000",
        "PROVIDER_RATE_PER_SECOND": "1000",
        "UPSTREAM_CONCURRENCY": "16",
    }
    env.update(overrides)
    return env


def start_api(redis_url: str, provider_url: str, workers: int, **overrides: str) -> tuple[subprocess.Popen[bytes], str]:
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    args = [sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--port", str(port)]
    args += ["--workers", str(workers), "--no-access-log", "--log-level", "warning"]
    return start(args, api_env(redis_url, provider_url, **overrides), f"{base}/health/ready"), base


def new_key(base: str, **limits: int) -> str:
    body = {"name": "benchmark", "rate_per_minute": 100000, "daily_limit": 1000000, **limits}
    r = httpx.post(f"{base}/admin/keys", headers=ADMIN, json=body)
    r.raise_for_status()
    return str(r.json()["api_key"])


def upstream_calls(provider_url: str) -> int:
    return int(httpx.get(f"{provider_url}/__control/state").json()["calls"])


async def measured(api_proc: subprocess.Popen[bytes], **kwargs: object) -> dict[str, object]:
    samples: list[float] = []
    cpu0, _ = cpu_seconds_and_rss_mb(api_proc.pid)
    t0 = time.monotonic()

    async def sample() -> None:
        while True:
            samples.append(cpu_seconds_and_rss_mb(api_proc.pid)[1])
            await asyncio.sleep(2)

    sampler = asyncio.create_task(sample())
    result = await run(**kwargs)  # type: ignore[arg-type]
    sampler.cancel()
    cpu1, rss = cpu_seconds_and_rss_mb(api_proc.pid)
    summary = result.summary()
    summary["api_cpu_percent_of_one_core"] = round(100 * (cpu1 - cpu0) / (time.monotonic() - t0), 1)
    summary["api_rss_mb_max"] = round(max([*samples, rss]), 1)
    return summary


def concurrency_checks(base: str) -> dict[str, object]:
    key = new_key(base, rate_per_minute=30)
    with httpx.Client(base_url=base, timeout=15) as client:
        import concurrent.futures

        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            codes = list(
                pool.map(
                    lambda i: (
                        client.get("/geocode", params={"q": f"burst {i}"}, headers={"X-API-Key": key}).status_code
                    ),
                    range(200),
                )
            )
        revoked = new_key(base)
        client.delete(f"/admin/keys/{revoked.split('_')[1]}", headers=ADMIN)
        with concurrent.futures.ThreadPoolExecutor(max_workers=50) as pool:
            revoked_codes = list(
                pool.map(
                    lambda i: (
                        client.get("/geocode", params={"q": f"r {i}"}, headers={"X-API-Key": revoked}).status_code
                    ),
                    range(100),
                )
            )
        missing = [client.get("/geocode", params={"q": "no key"}).status_code for _ in range(20)]
    return {
        "burst_200_requests_limit_30_per_minute": {
            "allowed": codes.count(200),
            "rejected_429": codes.count(429),
            "other": len(codes) - codes.count(200) - codes.count(429),
        },
        "revoked_key_100_concurrent": {"rejected_401": revoked_codes.count(401), "allowed": revoked_codes.count(200)},
        "missing_key_20_requests": {"rejected_401": missing.count(401), "allowed": missing.count(200)},
        "authorization_or_quota_failures": (codes.count(200) != 30)
        + (revoked_codes.count(200) > 0)
        + (missing.count(200) > 0),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=300)
    parser.add_argument("--rate", type=float, default=10)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--out", default="bench/results")
    args = parser.parse_args()

    workdir = tempfile.mkdtemp(prefix="geo-bench-")
    redis_port, fake_port = free_port(), free_port()
    redis_url = f"redis://127.0.0.1:{redis_port}/0"
    provider_url = f"http://127.0.0.1:{fake_port}"
    procs: list[subprocess.Popen[bytes]] = []
    report: dict[str, object] = {
        "environment": environment(args.workers),
        "settings": {"rate_rps": args.rate, "duration_s": args.duration, "upstream_delay_ms": UPSTREAM_DELAY_MS},
    }
    try:
        procs.append(
            start(["redis-server", "--port", str(redis_port), "--save", "", "--appendonly", "no", "--dir", workdir], {})
        )
        time.sleep(0.5)
        procs.append(
            start(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "tools.fake_upstream:app",
                    "--port",
                    str(fake_port),
                    "--log-level",
                    "warning",
                ],
                {"FAKE_UPSTREAM_DELAY_MS": str(UPSTREAM_DELAY_MS)},
                f"{provider_url}/__control/state",
            )
        )
        api_proc, base = start_api(redis_url, provider_url, args.workers)
        procs.append(api_proc)
        headers = {"X-API-Key": new_key(base)}
        hit_queries = [f"Cached place {i}" for i in range(50)]

        print("warm-up", flush=True)
        await run(base, "/geocode", lambda i: {"q": hit_queries[i % 50]}, rate=20, duration_s=5, headers=headers)
        calls_before = upstream_calls(provider_url)

        print(f"cache-hit workload: {args.rate} rps for {args.duration}s", flush=True)
        report["cache_hit"] = await measured(
            api_proc,
            base_url=base,
            path="/geocode",
            params_for=lambda i: {"q": hit_queries[i % 50]},
            rate=args.rate,
            duration_s=args.duration,
            headers=headers,
        )
        report["cache_hit"]["upstream_calls"] = upstream_calls(provider_url) - calls_before  # type: ignore[index]

        calls_before = upstream_calls(provider_url)
        run_id = int(time.time())
        print(f"cache-miss workload: {args.rate} rps for {args.duration}s", flush=True)
        report["cache_miss"] = await measured(
            api_proc,
            base_url=base,
            path="/geocode",
            params_for=lambda i: {"q": f"Uncached place {run_id} {i}"},
            rate=args.rate,
            duration_s=args.duration,
            headers=headers,
        )
        report["cache_miss"]["upstream_calls"] = upstream_calls(provider_url) - calls_before  # type: ignore[index]

        print("concurrency scenario", flush=True)
        report["concurrency"] = concurrency_checks(base)
        stop(api_proc)

        print("quota scenario", flush=True)
        httpx.post(f"{provider_url}/__control/reset")
        httpx.post(f"{provider_url}/__control/configure", json={"delay_seconds": UPSTREAM_DELAY_MS / 1000})
        quota_proc, quota_base = start_api(
            f"redis://127.0.0.1:{redis_port}/1", provider_url, args.workers, PROVIDER_DAILY_BUDGET="50"
        )
        procs.append(quota_proc)
        quota = await run(
            quota_base,
            "/geocode",
            lambda i: {"q": f"Quota place {run_id} {i}"},
            rate=args.rate,
            duration_s=15,
            headers={"X-API-Key": new_key(quota_base)},
        )
        report["quota"] = {
            **quota.summary(),
            "provider_daily_budget": 50,
            "upstream_calls": upstream_calls(provider_url),
            "budget_respected": upstream_calls(provider_url) <= 50,
        }
    finally:
        for proc in reversed(procs):
            stop(proc)
        shutil.rmtree(workdir, ignore_errors=True)

    out = ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = out / f"benchmark-{stamp}.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print(f"saved {path}")


if __name__ == "__main__":
    asyncio.run(main())
