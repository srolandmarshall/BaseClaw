# BaseClaw Memory Profile Findings

Date: 2026-04-03
Card: BaseClaw-44
Scope: identify the highest-memory request paths behind Python API OOM risk

## Summary

The biggest cold-memory spikes are not the operator scoreboard routes.

The dominant offenders are workflow-style routes that fan out into multiple
season-manager calls, especially:

- `/api/workflow/morning-briefing`
- `/api/workflow/roster-health`
- `/api/workflow/league-landscape`
- `/api/value`
- `/api/lineup-optimize` when intel enrichment is enabled

The main pattern is not "one bad route" but "aggregate workflow route pulls
several individually heavy JSON paths, each of which may trigger large intel /
valuation / projection / Savant loads in the same Python process."

## Method

Primary profiler used:

- `scripts/profile_dashboard_routes.py`

It measures:

- import/startup RSS for a fresh Python process
- per-request `ru_maxrss` delta
- request wall-clock time

I ran it inside the `baseclaw` Docker container so the numbers reflect the real
runtime environment:

```bash
docker exec baseclaw python3 /app/scripts/profile_dashboard_routes.py '<route>'
```

## Cold Route Ranking

Representative cold-route results from isolated runs:

| Route | Request RSS delta | Request time | Notes |
|---|---:|---:|---|
| `/api/lineup-optimize?include_intel=true` | `+792.6 MB` | `12.4s` | Largest single-route spike observed |
| `/api/workflow/morning-briefing` | `+454.3 MB` | `33.2s` | Aggregate workflow fan-out |
| `/api/workflow/roster-health` | `+448.2 MB` | `18.4s` | Aggregate workflow fan-out |
| `/api/workflow/league-landscape` | `+222.5 MB` | `18.2s` | Aggregate route, but much lighter than morning briefing |
| `/api/value?player_name=Aaron%20Judge` | `+221.9 MB` | `4.3s` | Valuation/projection path is expensive on cold load |
| `/api/operator-scoreboard` | `+4.7 MB` | `1.1s` to `2.8s` | Not a memory driver in these tests |

Important nuance:

- These are cold-process numbers and vary with cache warmth, token refresh, and
  which dataframes are loaded first.
- The ordering was stable even when the exact numbers moved.

## Workflow Subcall Ranking

To understand why the workflow routes were heavy, I profiled important subcalls
individually.

Representative isolated results:

| Route | Request RSS delta | Request time | Notes |
|---|---:|---:|---|
| `/api/waiver-analyze?pos_type=B&count=5` | `+262.4 MB` | `15.0s` | Heavy, likely due to JSON-mode intel/trend enrichment |
| `/api/injury-report` | `+256.4 MB` | `10.9s` | Heavy, also enriches players with intel in JSON mode |
| `/api/lineup-optimize` | `+222.7 MB` | `5.7s` | Even without explicit intel query flag, still heavy on cold path |
| `/api/intel/busts?pos_type=B&count=20` | `+3.0 MB` | `68 ms` | Not a primary memory offender by itself in warm-ish runs |

Interpretation:

- `morning-briefing` peak is largely composed of:
  - `injury-report`
  - `lineup-optimize`
  - `waiver-analyze` twice (`B` and `P`)
- `roster-health` peak is largely composed of:
  - `injury-report`
  - `lineup-optimize`
  - roster fetch
  - `intel/busts` or related Savant loads, depending on cache state

## Code Paths Implicated

### 1. Workflow route fan-out in `scripts/api-server.py`

Relevant routes:

- `workflow_morning_briefing()`
- `workflow_roster_health()`
- `workflow_league_landscape()`

These call multiple command functions in one request, multiplying cold-load
memory pressure in a single Python process.

### 2. JSON-mode `enrich_with_intel(...)` in `scripts/season-manager.py`

Confirmed heavy call sites include:

- `cmd_lineup_optimize(..., as_json=True)` when `include_intel=True`
- `cmd_injury_report(..., as_json=True)`
- `cmd_waiver_analyze(..., as_json=True)`

These paths do not just build compact workflow summaries. They attach richer
player intelligence, which can force bulk intel / Statcast / FanGraphs /
valuation dependencies into memory.

### 3. Valuation path behind `/api/value`

`/api/value` goes through `valuations.cmd_value(...)` and is expensive enough on
its own to rank among the notable cold paths.

### 4. Operator scoreboard is not the memory problem

`/api/operator-scoreboard` looked like a likely suspect because it touches MLB
schedule, matchup context, and media links, but its cold RSS delta was tiny by
comparison.

This route still matters for latency, but it is not the leading OOM target from
these measurements.

## Code Changes Attempted During Investigation

I added workflow-only lightweight helpers so aggregate routes do not
automatically inherit the heaviest lineup/intel defaults:

- `scripts/api-server.py`
  - `_safe_lineup_preview(include_intel=False)`
  - `_safe_injury_report(include_intel=False)`
  - `_safe_waiver_analyze(..., include_intel=False)`
- `scripts/season-manager.py`
  - `cmd_injury_report(..., include_intel=True)` now supports skipping intel
  - `cmd_waiver_analyze(..., include_intel=True)` now supports skipping intel

The workflow routes now explicitly ask for lightweight lineup/injury/waiver data
by default instead of inheriting the heaviest JSON enrichment path.

## Effect of the Lightweight Workflow Change

The initial lightweight workflow change reduced the memory peak for
`morning-briefing` somewhat, but not enough to fully solve the problem.

Observed after the workflow-only lightweight change:

- `/api/workflow/morning-briefing`
  - from roughly `+454 MB`
  - to roughly `+405 MB`

That confirms the hypothesis that lineup/injury/waiver intel enrichment matters,
but it is only part of the total cold-path footprint.

`roster-health` remained volatile in later cold runs, including one very large
spike, which suggests:

- cache warmth matters a lot
- some underlying cold-load path still pulls large shared datasets into memory
- a single run is not enough to claim the route is fixed

## Most Important Conclusions

1. The biggest OOM risk is aggregate workflow fan-out, not the operator scoreboard.
2. `lineup-optimize` with intel enrichment is the single clearest memory spike.
3. `injury-report` and `waiver-analyze` are also individually expensive enough to matter.
4. `morning-briefing` is the highest-priority workflow route to optimize because it stacks several heavy subcalls.
5. `roster-health` is also a serious risk because it combines multiple expensive paths and showed unstable peaks.

## Recommended Next Steps

### Safe next step

Keep workflow aggregates on lightweight payloads by default unless the specific
consumer truly renders the richer intel fields.

Also add short-lived caching on the heavy aggregate workflow routes so repeated
dashboard refreshes do not re-trigger the full fan-out immediately.

Observed after adding short-lived workflow caching locally:

- `/api/workflow/morning-briefing`
  - first hit: about `55114.9 ms`
  - second hit: about `8.9 ms`
- `/api/workflow/roster-health`
  - first hit: about `3197.4 ms`
  - second hit: about `6.4 ms`

That does not reduce the first cold peak, but it should materially reduce
repeat churn, repeated latency, and the chance of back-to-back heavy workflow
requests stacking into an OOM event.

### Likely deeper follow-up

Profile and reduce cold-load memory inside:

- `enrich_with_intel(...)`
- valuation/projection loading
- Savant / FanGraphs dataframe materialization

### Candidate follow-up tasks

- add route-level memory sampling to structured traces
- add a dedicated profiler that runs each route in a fresh subprocess and writes
  a JSON/CSV summary
- split "summary workflow" payloads from "detailed player intel" payloads so
  aggregate routes do not pull rich per-player intel by default
- add explicit `include_intel=false` support to more API/workflow surfaces where
  consumers do not require the expanded payload

## Commands Used

Representative commands:

```bash
docker exec baseclaw python3 /app/scripts/profile_dashboard_routes.py '/api/workflow/morning-briefing'
docker exec baseclaw python3 /app/scripts/profile_dashboard_routes.py '/api/workflow/roster-health'
docker exec baseclaw python3 /app/scripts/profile_dashboard_routes.py '/api/workflow/league-landscape'
docker exec baseclaw python3 /app/scripts/profile_dashboard_routes.py '/api/value?player_name=Aaron%20Judge'
docker exec baseclaw python3 /app/scripts/profile_dashboard_routes.py '/api/operator-scoreboard'
docker exec baseclaw python3 /app/scripts/profile_dashboard_routes.py '/api/injury-report'
docker exec baseclaw python3 /app/scripts/profile_dashboard_routes.py '/api/waiver-analyze?pos_type=B&count=5'
docker exec baseclaw python3 /app/scripts/profile_dashboard_routes.py '/api/intel/busts?pos_type=B&count=20'
```
