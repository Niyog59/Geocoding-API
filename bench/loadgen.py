"""Fixed-rate load generator.

Sends requests at a constant arrival rate (an open model): a new request
starts every 1/rate seconds whether or not earlier ones have finished, so a
slow server shows up as higher latency instead of quietly lowering the load.
"""

from __future__ import annotations

import asyncio
import statistics
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx


@dataclass
class Result:
    latencies_ms: list[float] = field(default_factory=list)
    statuses: Counter[str] = field(default_factory=Counter)
    cache: Counter[str] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    late_starts: int = 0
    started: float = 0.0
    finished: float = 0.0

    def summary(self) -> dict[str, object]:
        lat = sorted(self.latencies_ms)

        def pct(p: float) -> float:
            if not lat:
                return float("nan")
            return round(lat[min(len(lat) - 1, max(0, round(p / 100 * len(lat)) - 1))], 1)

        total = sum(self.statuses.values())
        duration = self.finished - self.started
        server_errors = sum(n for s, n in self.statuses.items() if s.startswith("5"))
        return {
            "requests": total,
            "duration_s": round(duration, 1),
            "throughput_rps": round(total / duration, 2) if duration else 0,
            "p50_ms": pct(50),
            "p95_ms": pct(95),
            "p99_ms": pct(99),
            "max_ms": round(lat[-1], 1) if lat else None,
            "mean_ms": round(statistics.fmean(lat), 1) if lat else None,
            "status_codes": dict(sorted(self.statuses.items())),
            "cache": dict(sorted(self.cache.items())),
            "client_errors": dict(self.errors),
            "server_error_rate": round(server_errors / total, 4) if total else 0,
            "late_starts": self.late_starts,
        }


async def run(
    base_url: str,
    path: str,
    params_for: Callable[[int], dict[str, str]],
    rate: float,
    duration_s: float,
    headers: dict[str, str] | None = None,
    timeout_s: float = 10,
) -> Result:
    result = Result()
    interval = 1 / rate
    total = int(rate * duration_s)
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
    async with httpx.AsyncClient(base_url=base_url, headers=headers, limits=limits, timeout=timeout_s) as client:

        async def one(i: int) -> None:
            start = time.perf_counter()
            try:
                response = await client.get(path, params=params_for(i))
                result.latencies_ms.append((time.perf_counter() - start) * 1000)
                result.statuses[str(response.status_code)] += 1
                result.cache[response.headers.get("X-Cache", "none")] += 1
            except httpx.HTTPError as exc:
                result.statuses["error"] += 1
                result.errors[type(exc).__name__] += 1

        result.started = time.perf_counter()
        tasks = []
        for i in range(total):
            target = result.started + i * interval
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            elif delay < -0.05:
                result.late_starts += 1  # the generator itself fell behind schedule
            tasks.append(asyncio.create_task(one(i)))
        await asyncio.gather(*tasks)
        result.finished = time.perf_counter()
    return result
