"""
S3-backed cold cache layer.

Sits between the in-memory caches in intel.py / valuations.py and the
upstream data sources (Baseball Savant, FanGraphs, etc.).  Stores
gzip-compressed blobs in S3 with TTL encoded as object metadata.

Usage:
    from s3_cache import s3_cache          # singleton
    raw = s3_cache.get("savant/statcast/2026-03-12/pitcher.csv.gz")
    if raw is None:
        raw = fetch_from_source()
        s3_cache.put("savant/statcast/2026-03-12/pitcher.csv.gz", raw, ttl_seconds=21600)

If S3_BUCKET is not set the cache is a silent no-op (safe for local dev).
"""

import gzip
import io
import os
import time
import threading

_BUCKET = os.environ.get("S3_BUCKET", "")
_REGION = os.environ.get("AWS_REGION", "us-east-1")

_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if not _BUCKET:
        return None
    with _client_lock:
        if _client is None:
            try:
                import boto3
                _client = boto3.client("s3", region_name=_REGION)
            except Exception as e:
                print(f"[s3_cache] boto3 unavailable: {e}")
                return None
    return _client


class S3Cache:
    """TTL-aware S3 blob cache with gzip compression."""

    _TTL_META_KEY = "expires-at"   # stored in S3 object metadata

    def get(self, key: str) -> bytes | None:
        """Return decompressed bytes if key exists and hasn't expired, else None."""
        client = _get_client()
        if client is None:
            return None
        try:
            resp = client.get_object(Bucket=_BUCKET, Key=key)
            expires_at = float(resp["Metadata"].get(self._TTL_META_KEY, "0"))
            if expires_at and time.time() > expires_at:
                return None
            compressed = resp["Body"].read()
            return gzip.decompress(compressed)
        except Exception:
            return None

    def put(self, key: str, data: bytes, ttl_seconds: int = 21600) -> bool:
        """Compress and upload data; returns True on success."""
        client = _get_client()
        if client is None:
            return False
        try:
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
                gz.write(data)
            compressed = buf.getvalue()
            expires_at = str(int(time.time() + ttl_seconds))
            client.put_object(
                Bucket=_BUCKET,
                Key=key,
                Body=compressed,
                ContentEncoding="gzip",
                ContentType="application/octet-stream",
                Metadata={self._TTL_META_KEY: expires_at},
            )
            return True
        except Exception as e:
            print(f"[s3_cache] put failed for {key}: {e}")
            return False

    def enabled(self) -> bool:
        return bool(_BUCKET) and _get_client() is not None


# Singleton used throughout the app
s3_cache = S3Cache()
