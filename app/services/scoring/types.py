"""Shared scoring DTOs and the canonical rationale_json schema.

SupplierScoreRationale is the only writer permitted for CompanyScore.rationale_json.
Never write unstructured dicts to that column — use SupplierScoreRationale(...).model_dump().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from pydantic import BaseModel


@dataclass
class ScoreResult:
    """Legacy numeric score 0-100 plus human-readable rationale bullets.

    Used by standalone utility functions that have not yet been migrated to
    return raw floats.  New pillar functions return float directly.
    """

    score: float
    rationale: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.score = max(0.0, min(100.0, float(self.score)))


# ---------------------------------------------------------------------------
# Canonical shape for CompanyScore.rationale_json
# ---------------------------------------------------------------------------

class ScoringInputs(BaseModel):
    supplier_id: str
    evidence_window_days: int
    scoring_version: str
    run_id: Optional[str] = None


class ComponentScores(BaseModel):
    material: float
    geopolitical: float
    regulatory: float
    operational: float
    financial: float
    overall: float


class DecayParameters(BaseModel):
    eval_date: str              # ISO date string (YYYY-MM-DD)
    material_window: int
    geopolitical_window: int
    regulatory_window: int
    operational_half_life: int


class SupplierScoreRationale(BaseModel):
    inputs: ScoringInputs
    components: ComponentScores
    top_evidence: list[str]     # event IDs that most influenced the score
    decay: DecayParameters
    notes: str                  # human-readable summary for UI display
