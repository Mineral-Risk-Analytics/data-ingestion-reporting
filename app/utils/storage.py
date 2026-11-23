"""Storage backends.

Two distinct storage lifecycles, deliberately separate:

* ``LocalFilesystemStorage`` — ingestion pipeline's raw-body archive under
  ``STORAGE_ROOT/raw/``. Local-only by design; audit-trail data doesn't need
  to leave the machine and rewriting the pipeline for cloud storage isn't
  justified. Callers: ``services/ingestion/pipeline.py``.

* ``InsightAssetStorage`` (Protocol) with two implementations —
  ``R2InsightStorage`` (Cloudflare R2 via boto3) and
  ``LocalInsightStorage`` (dev-only fallback). Persists user-facing insight
  post images (extracted from partner docx uploads). Returns the FINAL
  public ``src`` URL to embed in the article body — the caller does not
  know or care about the backend. Callers:
  ``services/content/docx_convert.py``, ``scripts/ingest_insight_docx.py``.

The split exists because insight images MUST survive Railway container
restarts (public URLs baked into published article bodies), whereas the
ingestion raw-body archive is regenerable and Railway-ephemeral is fine.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings

_log = logging.getLogger(__name__)


class RawStorageBackend(Protocol):
    """Contract for persisting raw API bodies and future object storage."""

    def write_bytes(self, relative_key: str, data: bytes, *, suffix: str = "") -> str:
        """Persist bytes under a stable relative key; return path or URI."""
        ...

    def read_bytes(self, storage_path: str) -> bytes:
        ...


class LocalFilesystemStorage:
    """Phase 1: store under STORAGE_ROOT (e.g. ./storage/raw/...)."""

    def __init__(self, root: str | Path | None = None) -> None:
        base = Path(root) if root is not None else Path(get_settings().storage_root)
        self.root = base.resolve()
        self.raw_root = self.root / "raw"
        self.raw_root.mkdir(parents=True, exist_ok=True)

    def write_bytes(self, relative_key: str, data: bytes, *, suffix: str = "") -> str:
        safe = relative_key.strip("/").replace("..", "_")
        path = self.raw_root / f"{safe}{suffix}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path.relative_to(self.root))

    def read_bytes(self, storage_path: str) -> bytes:
        path = self.root / storage_path
        return path.read_bytes()


def get_local_storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage()


# ---------------------------------------------------------------------------
# Insight post asset storage (user-facing images) — R2 in prod, local in dev
# ---------------------------------------------------------------------------


class InsightAssetStorage(Protocol):
    """Contract for storing public insight-post assets (image binaries).

    Returns the final ``src`` URL to embed in the article body. Backends
    decide whether that's a public R2 URL or a proxied ``/content-assets/``
    path; callers don't branch on backend type.
    """

    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        """Persist ``data`` at ``key``; return the public ``src`` URL."""
        ...


class LocalInsightStorage:
    """Dev fallback: writes under STORAGE_ROOT/raw/ and returns the
    ``/content-assets/`` proxied URL (served by
    ``app/api/routes/content_assets.py``). Local-only — Railway's ephemeral
    disk means anything persisted here vanishes on container restart, which
    is why we use R2 in prod (see get_insight_asset_storage below)."""

    def __init__(self) -> None:
        self._local = get_local_storage()

    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        # content_type unused — LocalFilesystemStorage infers from extension
        stored = self._local.write_bytes(key, data)
        return f"/content-assets/{stored}"


class R2InsightStorage:
    """Cloudflare R2 backend for insight assets.

    Uploads directly with ``boto3`` (S3-compatible, no presign needed since
    we're server-side) and returns ``{R2_PUBLIC_BASE_URL}/{key}``. Requires
    the R2 bucket to be public or fronted by a Worker/domain that serves
    objects — matches the existing report-PDF flow at
    ``app/api/routes/intelligence.py:upload_pdf`` (2026-07-22).
    """

    def __init__(
        self,
        account_id: str,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        public_base_url: str,
    ) -> None:
        self._bucket = bucket
        self._public_base = public_base_url.rstrip("/")
        import boto3  # lazy: only imported when R2 is actually configured

        self._client = boto3.client(
            "s3",
            endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
            region_name="auto",
        )

    def put_bytes(self, key: str, data: bytes, content_type: str) -> str:
        self._client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentType=content_type or "application/octet-stream",
            # public bucket assumed; if fronted by a Worker, ACL is ignored anyway
        )
        return f"{self._public_base}/{key}"


def get_insight_asset_storage() -> InsightAssetStorage:
    """Return the configured insight-asset backend.

    R2 when all 5 ``R2_*`` env vars are populated; local fallback otherwise.
    Log the selection once at startup so a misconfigured deploy is visible
    in Railway logs (2026-07-28 debugging: silent local-fallback on prod was
    what made image-write failures look mysterious)."""
    s = get_settings()
    if all(
        (s.r2_account_id, s.r2_access_key_id, s.r2_secret_access_key,
         s.r2_bucket, s.r2_public_base_url)
    ):
        _log.info("InsightAssetStorage=R2 bucket=%s", s.r2_bucket)
        return R2InsightStorage(
            account_id=s.r2_account_id,
            access_key_id=s.r2_access_key_id,
            secret_access_key=s.r2_secret_access_key,
            bucket=s.r2_bucket,
            public_base_url=s.r2_public_base_url,
        )
    _log.warning(
        "InsightAssetStorage=Local (R2_* env vars not fully set — "
        "images won't persist across Railway container restarts)"
    )
    return LocalInsightStorage()
