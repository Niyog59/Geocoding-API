# Findings checklist

This list was compiled from a review of the original `main.py` (commit
[`ccce8dc`](https://github.com/Niyog59/Geocoding-API/commit/ccce8dc)). The
separate review PDF was not available while this work was done, so the
numbering is this project's own; it may need to be matched to the PDF's
numbering.

Each finding has a regression test in `tests/test_regressions.py`. The tests
were added first, marked as expected failures
([`bc215d0`](https://github.com/Niyog59/Geocoding-API/commit/bc215d0)), and
each fix commit removed the marker for the findings it fixed.

| # | Finding | Fix | Commit | Tests |
|---|---|---|---|---|
| F1 | A hard-coded demo key (`geo_demo_key_for_testing_only`) was always active, logged at startup and published in the README. | Removed. The playground uses separately limited `/demo` routes instead of a shared key. | [`7d938f6`](https://github.com/Niyog59/Geocoding-API/commit/7d938f6) | `test_f1_*`, `test_keyed_routes_still_require_a_key_when_demo_is_enabled` |
| F2 | API keys and the master key were accepted in the query string. | Keys only in `X-API-Key`, admin secret only in `X-Admin-Secret`; credential-like query parameters return 400 `credentials_in_url`. | [`7d938f6`](https://github.com/Niyog59/Geocoding-API/commit/7d938f6) | `test_f2_*` |
| F3 | The master key was compared with `!=`, keys could not be listed or revoked, and admin took its secret as a query parameter. | `hmac.compare_digest`; random `gk_<id>_<secret>` keys stored as SHA-256 hashes, shown once, listable and revocable; admin and API credentials kept separate; per-key limits validated. | [`7d938f6`](https://github.com/Niyog59/Geocoding-API/commit/7d938f6) | `test_f3_*`, `tests/test_auth.py` |
| F4 | Insecure defaults were used silently (CORS `*` with credentials, keys optional, no master key, a personal email as User-Agent). | Production refuses weak admin secrets, local Redis and wildcard CORS; credentials are never allowed in CORS; keys required by default; no default contact email. | [`7d938f6`](https://github.com/Niyog59/Geocoding-API/commit/7d938f6), [`6262bb6`](https://github.com/Niyog59/Geocoding-API/commit/6262bb6) | `test_f4_*`, `test_production_*`, `test_wildcard_cors_is_rejected` |
| F5 | Keys, usage, cache and limits lived in process memory: lost on restart and different in every worker. | Keys, usage, limits, cache and provider budget moved to Redis. | [`7d938f6`](https://github.com/Niyog59/Geocoding-API/commit/7d938f6), [`680da62`](https://github.com/Niyog59/Geocoding-API/commit/680da62), [`a0196b1`](https://github.com/Niyog59/Geocoding-API/commit/a0196b1) | `test_f5_*`, `tests/integration/test_processes.py` |
| F6 | Per-key limits were stored but never applied; autocomplete gave anonymous callers the authenticated limit; limits were per worker. | Atomic Redis fixed-window limits per key (its own values), per anonymous IP and per demo visitor, with a shared demo total; accurate headers. | [`680da62`](https://github.com/Niyog59/Geocoding-API/commit/680da62) | `test_f6_*`, `tests/test_limits.py`, integration concurrency test |
| F7 | Public `nominatim.openstreetmap.org` was the default upstream with no shared budget or throttle, and autocomplete used it against its policy. | Shared daily and per-second provider budget for every caller (retries included); `PROVIDER` must be chosen; public Nominatim refused; Geoapify adapter added; autocomplete disabled (501). | [`680da62`](https://github.com/Niyog59/Geocoding-API/commit/680da62), [`6262bb6`](https://github.com/Niyog59/Geocoding-API/commit/6262bb6) | `test_f7_*`, `tests/test_providers.py`, `test_retries_count_against_the_provider_budget` |
| F8 | The forward-geocoding cache key ignored `viewbox`. | The cache key covers operation, provider, normalised query, limit, countries, viewbox and language. | [`a0196b1`](https://github.com/Niyog59/Geocoding-API/commit/a0196b1) | `test_f8_*`, `test_equivalent_inputs_share_an_entry_and_different_inputs_do_not` |
| F9 | The cache was in memory with an O(n log n) eviction sort, no protection against simultaneous identical misses, and empty results cached like others. | Redis cache with expiry and a size-bounded index; separate TTL for empty results; errors never cached; coalescing within and across workers. | [`a0196b1`](https://github.com/Niyog59/Geocoding-API/commit/a0196b1), [`0a23d65`](https://github.com/Niyog59/Geocoding-API/commit/0a23d65) | `test_f9_*`, `tests/test_cache.py` |
| F10 | `a or b or c if display_name else "Unknown"` labelled results "Unknown" whenever `display_name` was missing. | Explicit fallback: provider label, then address parts, then coordinates. | [`a0196b1`](https://github.com/Niyog59/Geocoding-API/commit/a0196b1) | `test_f10_*` |
| F11 | Fallback IDs used Python's per-process randomised `hash()`. | Provider ID, or a SHA-256 of provider, coordinates and label. | [`a0196b1`](https://github.com/Niyog59/Geocoding-API/commit/a0196b1) | `test_f11_*` (two processes with different `PYTHONHASHSEED`) |
| F12 | Every result had an invented `relevance: 1.0` and `accuracy: "point"`, and responses were called Mapbox-compatible without tests. | No invented scores; Geoapify's own `rank.confidence` is passed through; documentation says "GeoJSON responses". | [`a0196b1`](https://github.com/Niyog59/Geocoding-API/commit/a0196b1), [`6262bb6`](https://github.com/Niyog59/Geocoding-API/commit/6262bb6) | `test_f12_*` |
| F13 | The log format required `request_id`, which third-party records lacked, so every uvicorn and httpx log line raised a logging error; URLs with credentials could be logged. | JSON logs with the request ID from a context variable added by a handler filter; redaction of keys and secrets; httpx URL logging off; access log without query strings. | [`22540ff`](https://github.com/Niyog59/Geocoding-API/commit/22540ff) | `test_f13_*`, `test_request_logs_are_json_with_request_id_and_no_query_or_secrets` |
| F14 | A new HTTP client per request, no retries, deadlines or concurrency limits, the provider's 429 returned as the caller's 429, and unstructured errors. | Shared pooled client in the lifespan; timeouts, deadline, bounded concurrency and queueing; bounded retries for transient failures with Retry-After; stable error codes with request IDs. | [`22540ff`](https://github.com/Niyog59/Geocoding-API/commit/22540ff) | `test_f14_*`, `tests/test_reliability.py` |
| F15 | The public `/` exposed key and cache counts; there was no readiness check. | `/health/live` and `/health/ready` (Redis only, no provider credits); admin-only metrics. | [`22540ff`](https://github.com/Niyog59/Geocoding-API/commit/22540ff) | `test_f15_*`, `test_readiness_reports_redis_outage`, `test_metrics_require_admin_and_use_bounded_labels` |

## Remaining limitations related to the findings

* F6: fixed windows allow up to twice a per-minute limit across a minute
  boundary (for example 10 requests at 12:00:59 and 10 more at 12:01:00).
* F6: per-visitor demo limits on Render depend on how Render fills
  `X-Forwarded-For`; see "Client IP addresses" in
  [OPERATIONS.md](OPERATIONS.md). The shared daily total caps use either way.
* F7: the provider's own quota is tracked by this service's budget, not read
  from the provider. Other services using the same provider account are not
  counted, which is why the hosted budget is set well below the plan's quota.
