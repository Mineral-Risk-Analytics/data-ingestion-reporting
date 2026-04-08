"""Raw artifact storage — local filesystem now; swap for S3/R2 later."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from app.core.config import get_settings


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
