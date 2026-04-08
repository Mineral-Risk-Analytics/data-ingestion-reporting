from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models import IngestionRun
from app.schemas.ingestion import IngestionRunRead, IngestionTriggerRequest
from app.services.ingestion.pipeline import IngestionPipeline

router = APIRouter(tags=["ingestion"])


@router.post("/sources/{source_id}/ingest", response_model=IngestionRunRead)
def trigger_ingestion(
    source_id: int,
    body: Optional[IngestionTriggerRequest] = None,
    db: Session = Depends(get_db),
) -> IngestionRun:
    pipe = IngestionPipeline(db)
    params = (body.extra if body else None) or {}
    try:
        run_id = pipe.run(source_id, params=params)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    run = db.get(IngestionRun, run_id)
    if run is None:
        raise HTTPException(status_code=500, detail="Run not found after ingest")
    return run


@router.get("/ingestion-runs", response_model=list[IngestionRunRead])
def list_ingestion_runs(
    limit: int = 50,
    db: Session = Depends(get_db),
) -> list[IngestionRun]:
    q = select(IngestionRun).order_by(IngestionRun.started_at.desc()).limit(min(limit, 200))
    return list(db.scalars(q).all())
