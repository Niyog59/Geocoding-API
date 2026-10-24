# Operations: limits, quotas, failures and deployment

## Limits and quotas

All limits are enforced on the server, in Redis, by atomic scripts
(`app/limits.py`). Every worker and replica shares them.

| Who | Limit | Setting | Default | Hosted (render.yaml) |
|---|---|---|---|---|
| API key | requests per minute | per key, `DEFAULT_KEY_RATE_PER_MINUTE` | 60 | 60 |
| API key | requests per UTC day | per key, `DEFAULT_KEY_DAILY_LIMIT` | 1000 | 200 |
| Anonymous caller of `/geocode` (only if `REQUIRE_API_KEY=false`) | per minute / per day, per IP | `ANON_RATE_PER_MINUTE`, `ANON_DAILY_LIMIT` | 30 / 500 | keys required |
| Playground visitor (`/demo/*`) | per minute, per IP | `DEMO_RATE_PER_MINUTE` | 10 | 10 |
| Playground visitor | per UTC day, per IP | `DEMO_DAILY_PER_IP` | 100 | 50 |
| All playground visitors together | per UTC day | `DEMO_DAILY_TOTAL` | 1000 | 500 |
| Every caller, including keys | provider calls per UTC day | `PROVIDER_DAILY_BUDGET` | 2000 | 1000 |
| Every caller, including keys | provider calls per second | `PROVIDER_RATE_PER_SECOND` | 4 | 4 |

Cache hits count against the caller's own limits but not against the
provider budget. Retries count against the provider budget. Rejected requests
do not use up quota.

The hosted provider budget (1000 a day) is a third of Geoapify Free's 3000
credits, which leaves room for testing and for anything else on the same
account. Check the real remaining quota in the Geoapify dashboard before
raising it.

### Response headers

Successful and rate-limited responses carry:

| Header | Meaning |
|---|---|
| `X-RateLimit-Limit` | The limit of the window closest to running out |
| `X-RateLimit-Remaining` | Requests left in that window |
| `X-RateLimit-Reset` | Seconds until that window resets |
| `Retry-After` | On 429 and quota 503s: seconds until a retry can succeed |
| `X-Cache` | `HIT`, `MISS` or `SHARED` (waited for an identical request) |
| `X-Request-ID` | Matches the `request_id` in logs and error bodies |
| `X-Response-Time` | Time spent in the API, measured on the server |

Tests check that these values match what the server enforces
(`tests/test_limits.py`).

## Failure behaviour

Every error body has the same shape:

```json
{"error": {"code": "rate_limited", "message": "Too many requests. Please wait before trying again.", "request_id": "req_5d0c..."}}
```

| Situation | Status | `code` |
|---|---|---|
| Search with no matches | 200 | empty `features` |
| Reverse lookup with no nearby address | 200 | empty `features` |
| Invalid or unknown parameter | 400 | `invalid_request` |
| Credentials in the URL | 400 | `credentials_in_url` |
| Missing / invalid or revoked API key | 401 | `missing_api_key` / `invalid_api_key` |
| Caller over their limit | 429 | `rate_limited` |
| Demo total or provider budget used up for the day | 503 | `quota_exhausted` |
| Autocomplete requested | 501 | `autocomplete_disabled` |
| Provider timed out, or the request deadline passed | 504 | `upstream_timeout` |
| Provider returned an error / unreadable data | 502 | `upstream_error` / `upstream_bad_response` |
| Provider rate-limited us | 503 | `upstream_rate_limited` |
| Provider unreachable or rejected our credentials | 503 | `upstream_unavailable` |
| Too many provider calls already waiting | 503 | `upstream_busy` |
| Redis unavailable | 503 | `service_unavailable` |
| Anything unexpected | 500 | `internal_error` |

If Redis is unavailable, the API refuses requests instead of falling back to
unlimited in-memory behaviour. `/health/live` still answers (the process is
fine); `/health/ready` reports `redis: unavailable`.

Provider errors are never cached. Empty results are cached for
`EMPTY_CACHE_TTL_SECONDS` (one hour), other results for `CACHE_TTL_SECONDS`
(one day).

## Redis usage

Upstash bills a script as one command plus one for every Redis call inside
it, which is also how Redis's own `INFO commandstats` counts. Measured by
`tests/test_redis_cost.py`:

| Request | Billed commands (measured) |
|---|---|
| Playground search, cache hit | 5 |
| Playground search, cache miss | 13 |
| Keyed search, cache hit | 7 |
| Keyed search, cache miss | 15 |
| Request refused by a rate limit | 7 |

A counter's first use in each window adds one more command (setting its
expiry), so real averages are slightly higher.

The test fails if a change makes requests more expensive than this table.

**Does the hosted demo fit Upstash Free (500,000 commands a month)?** The demo
allows at most 500 searches a day in total. Even if every one were a cache
miss, that is at most about 500 × 15 × 31 ≈ 233,000 commands a month, under half of the free allowance. Health checks use
`/health/live`, which does not touch Redis. Requests refused by a limit still
cost a few commands each, so sustained abusive traffic could use up the
monthly allowance; Upstash Free then refuses commands, and the API answers
503 until the next month (it does not fall back to unlimited mode, and the
free plan has no card attached, so there is no overage bill).

## Redis persistence

* **Local:** `docker compose up` and `make redis` run Redis with an
  append-only file (`appendonly yes`, `appendfsync everysec`), so keys survive
  restarts. `tests/integration` checks this by restarting a Redis server with
  the same settings.
* **Hosted:** Upstash Redis keeps data durably. Do not use Render's free Key
  Value service for this: Render documents that it keeps data in memory only
  and loses it on restart.

## Metrics

`GET /metrics` (requires `X-Admin-Secret`) returns Prometheus metrics: request
counts by route and status class, request duration, provider calls by
outcome, provider call duration, cache lookups by outcome, and limit
rejections. Labels never include queries, coordinates, keys or request IDs.

The hosted service runs one worker (`WEB_CONCURRENCY=1`), so one scrape sees
everything. With several workers, set `PROMETHEUS_MULTIPROC_DIR` to an empty
directory before starting so the metrics of all workers are combined.

## Client IP addresses

Per-visitor demo limits need the visitor's IP address.

* `CLIENT_IP_SOURCE=peer` (default) uses the TCP connection address. Behind a
  proxy, that is the proxy, so every visitor would share one limit.
* `CLIENT_IP_SOURCE=x-forwarded-for` with `TRUSTED_PROXY_COUNT=N` takes the
  N-th address from the right of `X-Forwarded-For`, which is the address the
  outermost trusted proxy saw. Addresses further left can be forged by the
  client and are ignored.

Render's documentation does not describe exactly how its proxy fills
`X-Forwarded-For`, and community reports disagree, so `render.yaml` sets
`x-forwarded-for` with one proxy as an assumption. If it is wrong, the
consequence is limited to fairness: visitors may share or split per-visitor
limits, but the shared `DEMO_DAILY_TOTAL` and the provider budget still cap
total use. To check after deploying, send two requests from one machine with
different forged first entries (`X-Forwarded-For: 198.51.100.1` and
`198.51.100.2`) and confirm `X-RateLimit-Remaining` decreases across both.

## Deployment

The hosted version uses:

| Part | Service | Free-tier notes |
|---|---|---|
| Playground (static) | Cloudflare Pages | 500 builds a month; static files are served without a server, so the page loads even while the API sleeps |
| API | Render Free web service (Docker) | 750 instance hours a month; sleeps after 15 minutes without traffic and takes about a minute to wake; one instance |
| Shared state | Upstash Redis Free | 500,000 commands and 256 MB; data persisted |
| Provider | Geoapify Free | 3000 credits a day, 5 requests a second; "Powered by Geoapify" and OpenStreetMap attribution required |

There is no keep-alive traffic: the playground shows a waiting message while
the API wakes up.

### Steps

1. **Upstash:** create a Redis database (free plan, region near Render's
   Oregon region, TLS on). Copy the `rediss://` connection URL.
2. **Geoapify:** create a project and copy its API key. In the dashboard,
   restrict the key if you wish; it is only ever used by the API server.
3. **Render:** New > Blueprint, select this repository. Render reads
   `render.yaml`, generates `ADMIN_SECRET`, and asks for `GEOAPIFY_API_KEY`,
   `REDIS_URL` and `CORS_ORIGINS` (the Pages URL from step 4; you can update it
   after step 4). Health check path: `/health/live`.
4. **Cloudflare Pages:** connect the repository with root directory
   `frontend`, build command `npm ci && npm run build`, output directory
   `dist`, and environment variables `VITE_API_BASE_URL=https://<your service>.onrender.com`
   and `VITE_SOURCE_URL=https://github.com/Niyog59/Geocoding-API`. The build
   writes `_headers` with a content security policy that only allows that API
   origin.
5. Set `CORS_ORIGINS` on Render to the exact Pages origin, for example
   `https://geocoding-playground.pages.dev`.

### Creating an API key on the hosted service

The administrator secret is in the Render dashboard (Environment).

```bash
curl -X POST https://<service>.onrender.com/admin/keys \
  -H "X-Admin-Secret: $ADMIN_SECRET" -H "Content-Type: application/json" \
  -d '{"name": "my laptop", "rate_per_minute": 30, "daily_limit": 100}'
```

Geoapify's licence is not sublicensable, so keys on the hosted service are for
the project owner's own use. Other developers should run their own copy with
their own provider account.
