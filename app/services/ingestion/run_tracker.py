"""Ingestion run lifecycle: create, progress updates, completion."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models.enums import IngestionStatus
from app.models.ingestion import IngestionRun


class IngestionRunTracker:
    """Persist and update `IngestionRun` rows with structured stats."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def start(self, source_id: int, parameters: Optional[dict[str, Any]] = None) -> IngestionRun:
        run = IngestionRun(
            source_id=source_id,
            status=IngestionStatus.RUNNING.value,
            parameters_json=parameters,
            stats_json={"items_fetched": 0, "items_written": 0, "errors": []},
        )
        self._db.add(run)
        self._db.flush()
        return run

    def add_stat(self, run: IngestionRun, key: str, delta: int = 1) -> None:
        stats = dict(run.stats_json or {})
        stats[key] = int(stats.get(key, 0)) + delta
        run.stats_json = stats
        self._db.add(run)

    def record_error(self, run: IngestionRun, message: str) -> None:
        stats = dict(run.stats_json or {})
        errors = list(stats.get("errors") or [])
        errors.append(message)
        stats["errors"] = errors[-50:]
        run.stats_json = stats
        self._db.add(run)

    def complete(self, run: IngestionRun, status: IngestionStatus = IngestionStatus.SUCCESS) -> None:
        run.status = status.value
        run.completed_at = datetime.now(timezone.utc)
        self._db.add(run)

    def fail(self, run: IngestionRun, exc: Exception) -> None:
        run.status = IngestionStatus.FAILED.value
        run.completed_at = datetime.now(timezone.utc)
        run.error_message = str(exc)[:4000]
        self._db.add(run)
