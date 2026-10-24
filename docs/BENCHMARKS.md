# Benchmarks

## Method

`bench/run_benchmark.py` (`make bench`) starts an isolated stack on one
machine: a throwaway Redis server, the fake provider
(`tools/fake_upstream.py`) with a fixed 100 ms delay, and the API. Nothing
contacts a real geocoding provider or any shared service.

The load generator (`bench/loadgen.py`) sends requests at a fixed rate
whether or not earlier requests have finished (an open model), so a slow
server cannot hide its slowness by slowing the test down. Latency is measured
by the client, from sending the request to reading the whole response.

| Step | What it does |
|---|---|
| Warm-up | 5 seconds at 20 rps over 50 queries, which fills the cache |
| Cache hit | 5 minutes at 10 rps over the same 50 cached queries |
| Cache miss | 5 minutes at 10 rps, a new query for every request, so each calls the fake provider |
| Concurrency | 200 simultaneous requests against a key limited to 30 per minute; 100 simultaneous requests with a revoked key; 20 with no key |
| Quota | 15 seconds at 10 rps of new queries with a provider budget of 50 calls |

Results are saved as JSON in `bench/results/`, including the environment.

## Targets

| Target | Result (final run) |
|---|---|
| Cache hit p95 under 200 ms | 6.3 ms |
| Cache miss p95 under 500 ms (with a 100 ms provider) | 111.6 ms |
| Under 1% errors in normal load | 0% in both workloads |
| No authorization or quota failures | 0 |

## Final run

Commit [`6024bbf`](https://github.com/Niyog59/Geocoding-API/commit/6024bbf),
result file [`benchmark-20261024-061659.json`](../bench/results/benchmark-20261024-061659.json).

| Environment | |
|---|---|
| Date | 2026-10-24 (UTC) |
| Machine | Cloud container, Intel Xeon 2.80 GHz, 2 logical CPUs, 7.8 GB RAM, Linux 6.18 (Ubuntu 24.04 userland) |
| Software | Python 3.13.17, FastAPI 0.162.0, uvicorn 0.54.0, httpx 0.28.1, redis-py 6.4.0, Redis server 7.0.15 |
| API | 1 uvicorn worker, access log off |
| Redis | Same machine, TCP on 127.0.0.1, persistence off |
| Provider | Local fake provider, fixed 100 ms delay |
| Load generator | `bench/loadgen.py` on the same machine, 10 requests per second, 5 minutes per workload |

| Workload | Requests | p50 | p95 | p99 | Max | 5xx errors | Provider calls | API CPU | API memory |
|---|---|---|---|---|---|---|---|---|---|
| Cache hit | 3000, all `200 HIT` | 4.6 ms | 6.3 ms | 8.6 ms | 350.4 ms | 0 | 0 | 2.6% of one core | 66 MB |
| Cache miss | 3000, all `200 MISS` | 108.2 ms | 111.6 ms | 114.4 ms | 139.4 ms | 0 | 3000 | 5.0% of one core | 67 MB |

The single 350 ms cache hit was one outlier (p99 was 8.6 ms); it did not
appear in the earlier run, and is most likely a pause on the shared
machine.

| Check | Result |
|---|---|
| 200 simultaneous requests, key limited to 30 per minute | 30 allowed, 170 got 429, 0 other |
| 100 simultaneous requests with a revoked key | 100 got 401, 0 allowed |
| 20 requests with no key | 20 got 401, 0 allowed |
| Quota: 150 new searches with a provider budget of 50 | 50 got 200, 100 got 503 `quota_exhausted`, exactly 50 provider calls |

## Earlier run

Commit [`a5fa869`](https://github.com/Niyog59/Geocoding-API/commit/a5fa869)
(FastAPI 0.115.6, uvicorn 0.34.0), same machine and settings:
[`benchmark-20261024-054627.json`](../bench/results/benchmark-20261024-054627.json).

| Workload | p50 | p95 | p99 | Max | 5xx errors | Provider calls |
|---|---|---|---|---|---|---|
| Cache hit (3000 requests) | 4.7 ms | 6.4 ms | 9.1 ms | 48.5 ms | 0 | 0 |
| Cache miss (3000 requests) | 108.9 ms | 112.7 ms | 116.5 ms | 182.5 ms | 0 | 3000 |

## Reading the results

* About 100 ms of every cache miss is the fake provider's fixed delay; the
  API itself adds a few milliseconds. A real provider is slower and less
  predictable, so real misses will take longer.
* In the quota scenario, the 503 responses are the intended result: once the
  budget of 50 calls was used, the API refused further misses instead of
  calling the provider. They are not counted as errors against the targets.
* These numbers come from one machine with Redis on the same host. On the
  hosted version, Redis (Upstash) is reached over the internet, which adds
  network time to every request, and Render Free has a small shared CPU.
  They show that the code is not the bottleneck, not how fast the hosted
  playground is.

## Hosted smoke test

Not run yet, because the service is not deployed. After deployment it should
be a few requests only (well inside the provider's free quota): one cold
request after the service has slept, then a few warm hits and misses, and the
`X-Forwarded-For` check in [OPERATIONS.md](OPERATIONS.md#client-ip-addresses).
No load test is run against the hosted service or the provider.

## Lighthouse

Lighthouse 13.5.0 against the production build (`vite preview`) on the same
machine, with Lighthouse's default simulated throttling, and with the API
not running (the page loads without it):

| Form factor | Performance | Accessibility | Best practices | SEO | FCP | LCP | TBT | CLS |
|---|---|---|---|---|---|---|---|---|
| Mobile | 100 | 100 | 100 | 100 | 1.0 s | 1.0 s | 0 ms | 0 |
| Desktop | 100 | 100 | 100 | 100 | 0.3 s | 0.3 s | 0 ms | 0 |

Lighthouse 13.5 also shows an experimental "Agentic browsing" category
(50 on both). It is still under development in Lighthouse and was not a
target here. These are lab results on a fast machine; the hosted page
should be measured again after deployment.

The Playwright tests also run axe accessibility checks (WCAG 2.1 A and AA
rules) on the playground before and after a search, with no violations.
