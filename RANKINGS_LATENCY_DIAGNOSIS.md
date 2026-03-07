# Rankings Latency Diagnosis Report

## Scope
- Endpoint: `/api/rankings`
- Matrix: `pos_type in {B,P}`, `count in {25,60}`
- Runs: 5 cold + 5 warm per case
- Environment: local first, then Fly production

## Benchmark Command
```bash
python3 scripts/bench_rankings_latency.py --base-url http://localhost:8766 --mode both --iterations 5
```

Optional cold reset override:
```bash
python3 scripts/bench_rankings_latency.py \
  --base-url http://localhost:8766 \
  --mode cold \
  --iterations 5 \
  --cold-reset-cmd "<restart-api-command>"
```

## Trace Controls
- `TRACE_RANKINGS=1`
- `TRACE_SLOW_MS=5000`
- `TRACE_SAMPLE_RATE=1.0`
- `TRACE_PLAYER_SAMPLE=5`

## Artifacts
- CSV runs: `tmp/rankings_latency_benchmark_<timestamp>.csv`
- Summary JSON: `tmp/rankings_latency_summary_<timestamp>.json`
- BaseClaw trace logs keyed by `request_id`
- Draft Research run logs keyed by `request_id` and `run_id`

## Findings
### Top 3 Bottlenecks
1. `<fill>`
2. `<fill>`
3. `<fill>`

### Stage Attribution Evidence
- Dominant stage:
- p95 duration contribution:
- Cache hit/miss profile:

### Reproduced Slow Scenario (`P,count=60`)
- Request IDs:
- End-to-end trace continuity:
- Failure/timeout signatures:

## Mitigation Options (proposal-only in this phase)
1. `<option>` — estimated gain `<x ms>`
2. `<option>` — estimated gain `<x ms>`
3. `<option>` — estimated gain `<x ms>`

## Rollout Notes
1. Local validation completed.
2. Deploy to Fly with full tracing for 24h (`TRACE_SAMPLE_RATE=1.0`).
3. Reduce volume after 24h:
   - increase `TRACE_SLOW_MS`
   - lower `TRACE_SAMPLE_RATE`
4. Verify SLO: warm-cache `/api/rankings` `P95 < 60s`.
