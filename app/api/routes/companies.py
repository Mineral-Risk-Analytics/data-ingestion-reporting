from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.models.company import Company
from app.schemas.company import CompanyRead

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=list[CompanyRead])
def list_companies(
    limit: int = 200,
    db: Session = Depends(get_db),
) -> list[Company]:
    q = select(Company).order_by(Company.canonical_name).limit(min(limit, 1000))
    return list(db.scalars(q).all())


@router.post("/{company_id}/rescore", status_code=202)
def trigger_rescore(
    company_id: uuid.UUID,
    db: Session = Depends(get_db),
) -> dict:
    """
    Manually trigger a rescore for a single company using current evidence.

    Returns 202 with the new CompanyScore row ID. Does not require an active
    ingestion run — useful for testing and for rescoring after formula changes.
    """
    from app.services.scoring.orchestrator import rescore_company

    try:
        score_row = rescore_company(db, company_id, run_id="manual")
        db.commit()
        return {"company_score_id": str(score_row.id), "status": "scored"}
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=str(exc)) from exc
