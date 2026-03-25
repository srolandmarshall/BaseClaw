#!/usr/bin/env python3
"""Measure startup and per-request RSS for dashboard-facing API routes."""

import importlib.util
import json
import os
import resource
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
DEFAULT_ROUTES = [
    "/api/roster",
    "/api/probable-pitchers?days=7",
    "/api/operator-scoreboard",
    "/api/hot-bat-free-agents?count=8",
    "/api/hot-hand-free-agent-pitchers?count=8",
    "/api/best-available?pos_type=B&count=25&include_intel=false",
    "/api/lineup-optimize",
]


def _load_env():
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.split("#", 1)[0].strip())


def _rss_mb():
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return rss / (1024 * 1024)
    return rss / 1024


def main():
    _load_env()
    os.environ.setdefault("DATA_DIR", str(ROOT / "data"))
    os.environ.setdefault("OAUTH_FILE", str(ROOT / "config" / "yahoo_oauth.json"))
    os.environ.setdefault("ENABLE_STARTUP_WARMUP", "false")
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))

    start_rss = _rss_mb()
    started_import = time.time()
    spec = importlib.util.spec_from_file_location("api_server_profile", SCRIPTS_DIR / "api-server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import_ms = (time.time() - started_import) * 1000
    post_import_rss = _rss_mb()

    client = module.app.test_client()
    routes = sys.argv[1:] or list(DEFAULT_ROUTES)
    for route in routes:
        before = _rss_mb()
        started_request = time.time()
        response = client.get(route)
        request_ms = (time.time() - started_request) * 1000
        after = _rss_mb()
        payload = response.get_json(silent=True)
        print(json.dumps({
            "route": route,
            "status": response.status_code,
            "import_ms": round(import_ms, 1),
            "startup_rss_mb": round(post_import_rss - start_rss, 1),
            "request_ms": round(request_ms, 1),
            "request_rss_mb": round(after - before, 1),
            "total_rss_mb": round(after, 1),
            "keys": sorted(list(payload.keys()))[:8] if isinstance(payload, dict) else None,
        }))


if __name__ == "__main__":
    main()
