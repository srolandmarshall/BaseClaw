# BaseClaw Fly Deployment Runbook

This file is for maintainers deploying BaseClaw to Fly.io.

## Scope

- Deploy the Dockerized BaseClaw service to Fly Machines.
- Keep MCP (`/mcp`) and API (`/health`, `/api/*`) available over HTTPS.
- Preserve runtime state that should survive deploys (OAuth/token files, data cache).

## Preflight

1. Confirm Fly auth:
   - `fly auth whoami`
2. Work from repo root:
   - `cd ~/Development/BaseClaw`
3. Confirm app boots locally before deploy:
   - `docker compose up -d --build`
   - `curl -fsS http://localhost:4951/health`

## Required Runtime Behavior

- BaseClaw must listen on `process.env.PORT` (already supported in `mcp-apps/main.ts`).
- Never force bind to port `80` inside the container.
- Python sidecar must remain reachable at `http://localhost:8766`.

## Fly App Setup

If app does not exist yet:

1. Create app and baseline `fly.toml`:
   - `fly launch --no-deploy`
2. Ensure service port mapping matches BaseClaw:
   - Set `internal_port` to `4951` in `fly.toml` (`[http_service]`).
3. Commit `fly.toml` once stable.

## Persistent Storage

BaseClaw writes runtime files to `/app/config` and `/app/data`.

Use Fly volumes so these survive restarts/deploys:

1. Create volumes (example names):
   - `fly volumes create baseclaw_config --size 1 --region <REGION> --app <APP>`
   - `fly volumes create baseclaw_data --size 5 --region <REGION> --app <APP>`
2. Add mounts in `fly.toml`:
   - `destination = "/app/config"` for config volume
   - `destination = "/app/data"` for data volume

## Multi-Machine Readiness

Treat multi-machine Fly scale-out as a runtime-state question first, not just a
traffic question.

Current state in this repo:

- OAuth read tokens can be synchronized through `YAHOO_OAUTH_BRIDGE_URL` and
  `YAHOO_OAUTH_BRIDGE_TOKEN`.
- Some cold-cache artifacts can be shared through S3 when `S3_BUCKET` is set.
- Browser-based Yahoo write operations still depend on the local session file
  at `/app/config/yahoo_session.json`.

Operational rule:

- Do not scale `baseclaw-mcp` above one Machine while `ENABLE_WRITE_OPS=true`
  unless browser write-session state has been externalized or write traffic is
  otherwise pinned to a single Machine.

Reason:

- With multiple Machines, read traffic can spread safely once shared state is in
  place.
- Today, write requests can become machine-dependent because the browser session
  is stored on local disk per Machine.

Safe mitigation available now:

- Keep request-based Fly concurrency limits explicit in `fly.toml` so one busy
  Machine sheds load before `/health` becomes intermittently unhealthy.

Future multi-machine rollout checklist:

1. Externalize or pin browser write-session state.
2. Verify `/health` and `/api/health` stay healthy while one Machine handles
   long valuation or projection-refresh requests.
3. Scale count intentionally, for example with `fly scale count 2 --app <APP>`.
4. Verify both Machines are healthy with `fly machines list --app <APP>`.
5. Re-test write operations explicitly before leaving `ENABLE_WRITE_OPS=true`.

## Recommended Low-Concurrency Shape

For roughly 1 to 3 operator users, the default recommendation is:

- one always-on Machine
- `shared` CPU
- `1` CPU
- `2048 MB` RAM
- request-based concurrency limits enabled

Reasoning:

- BaseClaw runs both the Node front door and the Python API sidecar in one Fly
  Machine.
- Current reliability pressure is driven more by bursty expensive requests and
  memory headroom than by sustained CPU saturation.
- For now, reducing memory below `2048 MB` is more likely to trade reliability
  away than to produce a worthwhile cost win.

Do not treat multi-machine scale-out as the default budget recommendation while
write operations still depend on machine-local Yahoo browser session state.

## Required Env/Secrets

Set at minimum:

- `YAHOO_CONSUMER_KEY`
- `YAHOO_CONSUMER_SECRET`
- `LEAGUE_ID`
- `TEAM_ID`
- `MCP_SERVER_URL` (public HTTPS origin, no trailing slash preferred)
- `MCP_AUTH_PASSWORD`

Optional flags:

- `ENABLE_WRITE_OPS` (`true` or `false`)
- `ENABLE_PREVIEW` (`true` or `false`)
- `AGENT_AUTONOMY` (`full-auto`, `semi-auto`, `manual`)

Set via Fly:

- `fly secrets set KEY=value --app <APP>`

## Deploy

1. Deploy:
   - `fly deploy --remote-only --app <APP>`
2. Verify machine is healthy:
   - `fly status --app <APP>`
   - `fly machines list --app <APP>`
3. Check logs:
   - `fly logs --app <APP>`

## Smoke Tests

Run after each deploy:

1. Health endpoint:
   - `curl -fsS https://<APP_HOST>/health`
2. MCP endpoint:
   - `curl -I https://<APP_HOST>/mcp`
   - Expect auth challenge/401 when unauthenticated (not 404/5xx).
3. If production recently showed slow responses:
   - `fly logs --app <APP> --no-tail`
   - Look for long `request_complete` durations, repeated projection refresh
     failures, `oauth bridge PUT failed` warnings, and Fly service health-check
     failures.

## Troubleshooting

### `listen tcp :80: bind: permission denied`

- Root cause: app binding to privileged port `80`.
- Fix: bind to `$PORT` only; verify `fly.toml` `internal_port` and app startup command.

### Proxy cannot find machines / restart loop / max restart attempts exhausted

1. `fly logs --app <APP>` to get first crash error.
2. `fly machines list --app <APP>` to inspect failed machine state.
3. Fix config/env and redeploy.

### MCP returns 401 from clients

- Verify client and server share the same `MCP_AUTH_PASSWORD`.
- Verify client is using the correct server URL (`https://<APP_HOST>/mcp`).

### API endpoints return 502/5xx

- Confirm Python sidecar is up (entrypoint starts `api-server.py`).
- Check logs for Python startup/import/runtime errors.

## Redeploy Semantics

- Code changes redeploy only when `fly deploy` is run (unless you have CI configured to do this).
- A successful deploy rolls new machines; data on mounted volumes persists.
