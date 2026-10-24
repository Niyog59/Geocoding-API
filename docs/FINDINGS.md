# Findings checklist

This list was compiled from a review of the original `main.py` (commit `ccce8dc`).
The separate review PDF was not available while this work was done, so the
numbering here is this project's own. Each finding links to its regression test
in `tests/test_regressions.py` and, once fixed, to the commit that fixed it.

| # | Finding | Test | Status |
|---|---------|------|--------|
| F1 | A hard-coded demo key (`geo_demo_key_for_testing_only`) was always active, and its value was logged and published in the README. | `test_f1_builtin_demo_key_is_rejected` | Open |
| F2 | API keys and the master key were accepted in the query string, where proxies, browser history and access logs record them. | `test_f2_credentials_in_query_string_are_rejected` | Open |
| F3 | The master key was compared with `!=` (not constant time), keys could not be listed or revoked, and the admin route took its secret as a query parameter. | `test_f3_*` | Open |
| F4 | Insecure defaults were used silently: CORS `*` with credentials, no master key, keys optional, and a personal contact email as the default User-Agent. | `test_f4_production_rejects_insecure_configuration` | Open |
| F5 | Keys, usage counters, the cache and rate limits lived in process memory, so they were lost on restart and differed between workers (the README recommended four Gunicorn workers). | `test_f5_keys_and_usage_survive_an_api_restart` | Open |
| F6 | Per-key rate limits were stored but never applied (every route used a fixed limit), autocomplete used the authenticated limit for anonymous callers, and limits were per worker. | `test_f6_*` | Open |
| F7 | The default upstream was the public `nominatim.openstreetmap.org` with no shared request budget or throttle, and the autocomplete route used it, which its usage policy forbids. | `test_f7_*` | Open |
| F8 | The forward-geocoding cache key ignored `viewbox`, so a request for one area could return cached results for another. | `test_f8_viewbox_is_part_of_the_cache_identity` | Open |
| F9 | The cache was unbounded in practice (an O(n log n) eviction sort per insert), had no protection against simultaneous identical misses, and cached empty results like any other. | `test_f9_identical_concurrent_requests_share_one_upstream_call` | Open |
| F10 | The label fallback `a or b or c if display_name else "Unknown"` is parsed as `(a or b or c) if display_name else "Unknown"`, so results without `display_name` were labelled "Unknown" even when address parts existed. | `test_f10_label_falls_back_to_address_parts` | Open |
| F11 | Fallback IDs used Python's `hash()`, which is randomised per process, so the same place had different IDs across workers and restarts. | `test_f11_feature_ids_are_stable_across_processes` | Open |
| F12 | Every result was given an invented `relevance: 1.0` and `accuracy: "point"`, and responses were described as Mapbox-compatible without tests for that claim. | `test_f12_no_invented_relevance_scores` | Open |
| F13 | The root log format required `request_id`, but only the module logger added it, so every third-party log line (uvicorn, httpx) raised a logging error. Upstream URLs could also be logged with credentials. | `test_f13_third_party_logs_format_cleanly_and_redact_credentials` | Open |
| F14 | A new HTTP client was created per request, there were no retries, deadlines or concurrency limits, upstream rate limits were passed to callers as their own 429, and error bodies had no stable structure or request ID. | `test_f14_upstream_failures_return_structured_errors` | Open |
| F15 | The public `/` health check exposed the number of loaded API keys and cache entries, and there was no readiness check for dependencies. | `test_f15_health_endpoints_do_not_expose_inventory_or_spend_credits` | Open |
