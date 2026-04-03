# BaseClaw-57 Fly Multi-Machine Investigation

## Question

Can BaseClaw safely run multiple Fly Machines to reduce slow responses and avoid
OOM-style instability?

## Findings

### Production shape on April 3, 2026

- `flyctl status -a baseclaw-mcp` showed exactly one started Machine in `iad`
  on version `46`.
- `flyctl volumes list -a baseclaw-mcp` showed no attached Fly volumes.
- `GET https://baseclaw-mcp.fly.dev/health` returned `503` at
  `2026-04-03T06:41:27Z` with `{"ok":false,"python_ok":false,"writes_enabled":true}`.

### Recent slow-response evidence

- Fly logs showed a slow `/api/value?player_name=Ben+Rice` request completing in
  about `7205 ms` at `2026-04-03T06:39:44Z`.
- That request spent time in repeated projection refresh failures, including
  FanGraphs `403` responses and a `valuation_projection_refresh_summary` event.
- Fly logs also recorded a failed service health check at
  `2026-04-03T06:40:14Z`, which lines up with the external `/health` failures.
- The same log window contained repeated `oauth bridge PUT failed: The read
  operation timed out` warnings, which suggests background state sync pressure
  during already-slow request windows.

## Multi-Machine Readiness

### Safe today

- Yahoo OAuth read tokens can be synced through the OAuth bridge in
  `scripts/shared.py`.
- Some cold-cache data is already externalized through S3 in `scripts/s3_cache.py`.
- Read-heavy traffic can benefit from per-machine load shedding once more than
  one Machine exists.

### Not safe today

- Browser-based write operations depend on a machine-local session file at
  `/app/config/yahoo_session.json` in `scripts/yahoo_browser.py`.
- There is no equivalent bridge or shared storage for that browser session.
- With `ENABLE_WRITE_OPS=true`, routing write requests across multiple Machines
  would make write success depend on which Machine receives the request.

## Recommendation

Do not scale production BaseClaw above one write-enabled Fly Machine yet.

The safe immediate mitigation is to cap per-machine HTTP request concurrency so
Fly stops overloading a single shared CPU VM before health checks begin to fail.
That change is included in this PR.

If Sam wants real multi-machine scale-out, the next slice should externalize or
pin browser write-session state first. After that, scale count can move to two
Machines and request-based Fly load balancing will become a reasonable
production mitigation.
