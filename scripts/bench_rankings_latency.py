#!/usr/bin/env python3
"""Benchmark /api/rankings latency matrix for cold/warm cache attribution."""

import argparse
import csv
import json
import os
import statistics
import subprocess
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime


MATRIX = [
    ("B", 25),
    ("B", 60),
    ("P", 25),
    ("P", 60),
]


def percentile(values, pct):
    if not values:
        return 0
    ordered = sorted(values)
    idx = int(round((pct / 100.0) * (len(ordered) - 1)))
    idx = max(0, min(len(ordered) - 1, idx))
    return ordered[idx]


def reset_cold_cache(base_url, endpoint, reset_cmd):
    if reset_cmd:
        subprocess.run(reset_cmd, shell=True, check=False)  # noqa: S602,S607
    if endpoint:
        url = base_url.rstrip("/") + endpoint
        req = urllib.request.Request(
            url,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=20).read()
        except Exception:
            pass


def run_once(base_url, pos_type, count, timeout_s, run_id):
    request_id = str(uuid.uuid4())
    query = urllib.parse.urlencode({"pos_type": pos_type, "count": str(count)})
    url = base_url.rstrip("/") + "/api/rankings?" + query
    req = urllib.request.Request(
        url,
        method="GET",
        headers={
            "X-Request-Id": request_id,
            "X-Research-Run-Id": run_id,
        },
    )

    started = time.perf_counter()
    status = 0
    body_size = 0
    error = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.getcode()
            payload = resp.read()
            body_size = len(payload)
    except Exception as exc:  # noqa: BLE001
        error = str(exc)
    duration_ms = int((time.perf_counter() - started) * 1000)

    return {
        "request_id": request_id,
        "pos_type": pos_type,
        "count": count,
        "status": status,
        "duration_ms": duration_ms,
        "body_size": body_size,
        "error": error,
    }


def summarize(rows):
    grouped = {}
    for row in rows:
        key = (row["mode"], row["pos_type"], row["count"])
        grouped.setdefault(key, []).append(row)

    summary = []
    for key, items in sorted(grouped.items()):
        durations = [x["duration_ms"] for x in items]
        failures = len([x for x in items if x["status"] < 200 or x["status"] >= 300])
        summary.append(
            {
                "mode": key[0],
                "pos_type": key[1],
                "count": key[2],
                "runs": len(items),
                "failures": failures,
                "avg_ms": int(statistics.mean(durations)) if durations else 0,
                "p50_ms": percentile(durations, 50),
                "p95_ms": percentile(durations, 95),
                "p99_ms": percentile(durations, 99),
                "max_ms": max(durations) if durations else 0,
            }
        )
    return summary


def main():
    parser = argparse.ArgumentParser(description="Benchmark /api/rankings latency matrix.")
    parser.add_argument("--base-url", default="http://localhost:8766")
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--mode", choices=["warm", "cold", "both"], default="both")
    parser.add_argument("--timeout-seconds", type=int, default=180)
    parser.add_argument("--cold-reset-endpoint", default="/api/cache-clear")
    parser.add_argument("--cold-reset-cmd", default="")
    parser.add_argument("--output-dir", default="tmp")
    args = parser.parse_args()

    modes = ["warm", "cold"] if args.mode == "both" else [args.mode]
    run_id = "bench-" + datetime.utcnow().strftime("%Y%m%d%H%M%S")
    rows = []

    for mode in modes:
        for pos_type, count in MATRIX:
            for run_idx in range(1, args.iterations + 1):
                if mode == "cold":
                    reset_cold_cache(args.base_url, args.cold_reset_endpoint, args.cold_reset_cmd)
                row = run_once(args.base_url, pos_type, count, args.timeout_seconds, run_id)
                row["mode"] = mode
                row["run"] = run_idx
                rows.append(row)
                print(
                    f"{mode:4} pos={pos_type} count={count:2d} run={run_idx} "
                    f"status={row['status']} duration_ms={row['duration_ms']} request_id={row['request_id']}"
                )

    summary = summarize(rows)
    os.makedirs(args.output_dir, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    csv_path = os.path.join(args.output_dir, f"rankings_latency_benchmark_{ts}.csv")
    json_path = os.path.join(args.output_dir, f"rankings_latency_summary_{ts}.json")

    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["mode", "run", "pos_type", "count", "status", "duration_ms", "body_size", "request_id", "error"],
        )
        writer.writeheader()
        writer.writerows(rows)

    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2)

    print("\nSummary:")
    for item in summary:
        print(
            f"{item['mode']:4} pos={item['pos_type']} count={item['count']:2d} "
            f"runs={item['runs']} fail={item['failures']} "
            f"p50={item['p50_ms']}ms p95={item['p95_ms']}ms p99={item['p99_ms']}ms max={item['max_ms']}ms"
        )
    print(f"\nWrote: {csv_path}")
    print(f"Wrote: {json_path}")


if __name__ == "__main__":
    main()
