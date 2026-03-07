#!/usr/bin/env python3
"""Structured trace helpers for rankings latency attribution."""

import json
import os
import random
import threading
import time
import uuid

_TRACE_CTX = threading.local()


def _env_bool(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def trace_config():
    return {
        "trace_rankings": _env_bool("TRACE_RANKINGS", False),
        "trace_slow_ms": max(0, _env_int("TRACE_SLOW_MS", 5000)),
        "trace_sample_rate": _clamp(_env_float("TRACE_SAMPLE_RATE", 1.0), 0.0, 1.0),
        "trace_player_sample": max(0, _env_int("TRACE_PLAYER_SAMPLE", 5)),
    }


def monotonic_ms():
    return int(time.perf_counter() * 1000)


def _safe_int(value, default=None):
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def set_trace_context(context):
    _TRACE_CTX.context = context


def update_trace_context(**kwargs):
    ctx = get_trace_context()
    ctx.update(kwargs)
    _TRACE_CTX.context = ctx


def get_trace_context():
    return dict(getattr(_TRACE_CTX, "context", {}) or {})


def clear_trace_context():
    _TRACE_CTX.context = {}


def start_request_trace(route, method, headers=None, args=None):
    headers = headers or {}
    args = args or {}
    cfg = trace_config()
    request_id = headers.get("X-Request-Id") or str(uuid.uuid4())
    trace_sampled = False
    if route == "/api/rankings" and cfg["trace_rankings"]:
        trace_sampled = random.random() < cfg["trace_sample_rate"]

    context = {
        "request_id": request_id,
        "research_run_id": headers.get("X-Research-Run-Id", ""),
        "route": route,
        "method": method,
        "pos_type": args.get("pos_type"),
        "count": _safe_int(args.get("count"), None),
        "request_started_ms": monotonic_ms(),
        "trace_sampled": trace_sampled,
    }
    set_trace_context(context)
    return context


def trace_request_id():
    ctx = get_trace_context()
    rid = ctx.get("request_id")
    if rid:
        return rid
    return str(uuid.uuid4())


def should_trace_rankings(duration_ms=None, context=None):
    cfg = trace_config()
    if not cfg["trace_rankings"]:
        return False
    ctx = context or get_trace_context()
    if ctx.get("trace_sampled"):
        return True
    if duration_ms is not None and duration_ms >= cfg["trace_slow_ms"]:
        return True
    return False


def _payload(event, stage, duration_ms, cache_hit, status, extra):
    ctx = get_trace_context()
    data = {
        "event": event,
        "request_id": ctx.get("request_id", ""),
        "route": ctx.get("route", ""),
        "pos_type": ctx.get("pos_type"),
        "count": ctx.get("count"),
        "duration_ms": duration_ms,
        "stage": stage,
        "cache_hit": cache_hit,
        "status": status,
    }
    data.update(extra)
    return data


def log_trace_event(
    event,
    stage,
    duration_ms,
    cache_hit=None,
    status="ok",
    gate="always",
    force=False,
    **extra
):
    if gate == "rankings" and not force:
        if not should_trace_rankings(duration_ms=duration_ms):
            return

    data = _payload(event, stage, duration_ms, cache_hit, status, extra)
    try:
        print(json.dumps(data, sort_keys=True, separators=(",", ":")))
    except Exception:
        pass
