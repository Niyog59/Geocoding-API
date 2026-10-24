"""Calls to the geocoding provider.

One HTTP client is created at startup and reused, so connections are pooled.
Every call:

* waits for a free slot (at most UPSTREAM_CONCURRENCY calls run at once, and a
  caller waits at most UPSTREAM_QUEUE_SECONDS for a slot);
* takes one unit from the shared provider budget, so retries are counted too;
* has explicit connect and read timeouts;
* is retried at most UPSTREAM_MAX_RETRIES times, only for failures that a retry
  can fix (connection errors, timeouts, 502/503/504 and 429), honouring the
  provider's Retry-After and never past the request deadline.

Provider failures become clear, stable error codes for callers. The provider's
own rate limit is reported as a 503 from this service, not as the caller's 429,
because the caller did nothing wrong.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any

import httpx

from app.errors import ApiError
from app.limits import ProviderBudget
from app.metrics import UPSTREAM_DURATION, UPSTREAM_REQUESTS

logger = logging.getLogger("app.upstream")

RETRYABLE_STATUS = {429, 502, 503, 504}


class UpstreamClient:
    def __init__(
        self,
        provider: str,
        budget: ProviderBudget,
        *,
        connect_timeout: float,
        read_timeout: float,
        concurrency: int,
        queue_seconds: float,
        max_retries: int,
        max_retry_after: float,
        budget_wait_seconds: float,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.provider = provider
        self.budget = budget
        self.queue_seconds = queue_seconds
        self.max_retries = max_retries
        self.max_retry_after = max_retry_after
        self.budget_wait_seconds = budget_wait_seconds
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._slots = asyncio.Semaphore(concurrency)
        self.http = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(read_timeout, connect=connect_timeout, pool=queue_seconds),
            limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def get_json(self, url: str, params: dict[str, Any], deadline: float) -> Any:
        """GET `url` and return parsed JSON. `deadline` is a time.monotonic() value."""
        try:
            await asyncio.wait_for(self._slots.acquire(), timeout=min(self.queue_seconds, _left(deadline)))
        except TimeoutError:
            raise ApiError(
                503, "upstream_busy", "The service is busy. Please try again shortly.", {"Retry-After": "1"}
            ) from None
        try:
            return await self._with_retries(url, params, deadline)
        finally:
            self._slots.release()

    async def _with_retries(self, url: str, params: dict[str, Any], deadline: float) -> Any:
        attempt = 0
        while True:
            await self.budget.acquire(min(self.budget_wait_seconds, _left(deadline)))
            outcome, value = await self._attempt(url, params, deadline)
            if outcome == "ok":
                return value
            error, retry_after = value
            if attempt >= self.max_retries or outcome == "final":
                raise error
            pause = retry_after if retry_after is not None else 0.2 * (2**attempt) + random.uniform(0, 0.1)
            if pause > self.max_retry_after or time.monotonic() + pause >= deadline - 0.5:
                raise error
            attempt += 1
            await asyncio.sleep(pause)

    async def _attempt(self, url: str, params: dict[str, Any], deadline: float) -> tuple[str, Any]:
        remaining = _left(deadline)
        if remaining <= 0:
            raise ApiError(504, "upstream_timeout", "The geocoding provider did not respond in time.")
        started = time.perf_counter()
        try:
            timeout = httpx.Timeout(min(remaining, self.read_timeout), connect=min(remaining, self.connect_timeout))
            # wait_for enforces the limit even where the transport does not.
            response = await asyncio.wait_for(
                self.http.get(url, params=params, timeout=timeout),
                timeout=min(remaining, self.connect_timeout + self.read_timeout),
            )
        except (httpx.TimeoutException, TimeoutError):
            return self._failed(
                "timeout", started, 504, "upstream_timeout", "The geocoding provider did not respond in time."
            )
        except httpx.RequestError:
            return self._failed(
                "connect_error", started, 503, "upstream_unavailable", "The geocoding provider could not be reached."
            )
        status = response.status_code
        if status == 200:
            try:
                data = response.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return self._failed(
                    "bad_response",
                    started,
                    502,
                    "upstream_bad_response",
                    "The geocoding provider sent an unreadable response.",
                    final=True,
                )
            self._record("ok", started)
            return "ok", data
        retry_after = _retry_after(response.headers.get("Retry-After"))
        if status == 429:
            headers = {"Retry-After": str(max(1, round(retry_after or 1)))}
            return self._failed(
                "rate_limited",
                started,
                503,
                "upstream_rate_limited",
                "The geocoding provider is limiting requests. Please try again shortly.",
                retry_after=retry_after,
                headers=headers,
            )
        if status in (401, 403):
            logger.error(
                "Provider rejected our credentials or access (HTTP %s); check the provider configuration", status
            )
            return self._failed(
                "auth_error", started, 503, "upstream_unavailable", "The geocoding provider is unavailable.", final=True
            )
        final = status not in RETRYABLE_STATUS
        return self._failed(
            f"http_{status // 100}xx",
            started,
            502,
            "upstream_error",
            "The geocoding provider returned an error.",
            retry_after=retry_after,
            final=final,
        )

    def _record(self, outcome: str, started: float) -> None:
        UPSTREAM_REQUESTS.labels(self.provider, outcome).inc()
        UPSTREAM_DURATION.labels(self.provider).observe(time.perf_counter() - started)

    def _failed(
        self,
        outcome: str,
        started: float,
        status: int,
        code: str,
        message: str,
        *,
        retry_after: float | None = None,
        headers: dict[str, str] | None = None,
        final: bool = False,
    ) -> tuple[str, Any]:
        self._record(outcome, started)
        logger.warning("Provider call failed: %s", outcome)
        return ("final" if final else "retry"), (ApiError(status, code, message, headers), retry_after)


def _left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: treat as unknown rather than guessing
