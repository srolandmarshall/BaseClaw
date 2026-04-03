# BaseClaw-47 Fly Shape Recommendation

## Scope

This recommendation is for a low-concurrency BaseClaw deployment serving about
1 to 3 operator users at a time.

It assumes the current product shape:

- one Fly app serving both the Node MCP layer and the Python API sidecar
- `ENABLE_WRITE_OPS=true`
- operator traffic is bursty, not constant high-throughput traffic

## Recommended Default

Use a single always-on Fly Machine with:

- `shared` CPU
- `1` CPU
- `2048 MB` RAM
- `min_machines_running = 1`
- request-based concurrency limits enabled in `fly.toml`

This matches the current production shape and is the cheapest reasonable
default that still preserves headroom for the mixed Node + Python process model.

## Why This Is The Right Default

### Memory

BaseClaw runs:

- the Express MCP/API front door
- the Python API server
- valuation, projection, and scoreboard work inside Python
- optional browser-write paths and token/session management

That mix makes memory pressure more important than raw CPU for the current
operator workload. Dropping below `2048 MB` would save money, but it would also
remove the small amount of headroom that currently separates normal operator use
from restart-risk during heavier request bursts.

### Reliability

Recent production observations on April 3, 2026 showed:

- health-check instability under slow-request pressure
- repeated expensive `/api/operator-scoreboard` requests in roughly the
  `2.1s` to `3.2s` range
- prior slow `/api/value` requests and transient health degradation

That points to a service that is still sensitive to bursty expensive work.
Shrinking the box would be a cost optimization in the wrong direction.

### Multi-Machine Constraint

Do not treat multiple Machines as the default low-cost reliability plan yet.

With `ENABLE_WRITE_OPS=true`, browser-based Yahoo write operations still depend
on machine-local session state. Until that state is externalized or write
traffic is pinned, multi-machine routing increases correctness risk.

## Scaling Stance

For 1 to 3 operator users:

- stay on `1x shared-cpu-1x / 2048 MB`
- keep one Machine always running
- keep request concurrency capped
- optimize expensive routes before changing topology

Only move beyond that if one of these becomes true:

1. repeated health-check failures return under normal operator traffic
2. memory-related restarts continue after route-level optimization work
3. write-session state is externalized, making multi-machine routing safe

## What Not To Do Yet

- do not scale down below `2048 MB` just to save cost
- do not add a second write-enabled Machine as the default
- do not move to a more expensive CPU class before finishing the current route
  and caching follow-ups

## Next Recommended Follow-Ups

If more reliability is needed, prefer this order:

1. profile highest-memory request paths
2. cache or precompute repeated expensive artifacts
3. improve degraded-mode behavior for expensive dependency failures
4. only then reconsider a larger box or multi-machine topology

## Bottom Line

For the current BaseClaw service and workload, the cheapest reliable Fly shape
is the one already in production:

- `shared` 1 CPU
- `2048 MB` RAM
- one always-on Machine
- explicit request concurrency limits

That is the right default until the expensive-route and write-state follow-up
cards are completed.
