"""Shared scoring DTOs and the canonical rationale_json schema.

SupplierScoreRationale is the only writer permitted for CompanyScore.rationale_json.
Never write unstructured dicts to that column — use SupplierScoreRationale(...).model_dump().
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, ClassVar, Optional

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
    supply_chain_propagation: Optional[float] = None
    # ``None`` means "no propagation signal available" (no suppliers in graph,
    # or scoped run that disabled the pillar). The orchestrator normalises the
    # remaining five weights so the overall score is not artificially deflated.


class DecayParameters(BaseModel):
    eval_date: str              # ISO date string (YYYY-MM-DD)
    material_window: int
    geopolitical_window: int
    regulatory_window: int
    operational_half_life: int


class PropagationContribution(BaseModel):
    """Single supplier's contribution to the supply_chain_propagation pillar."""

    supplier_id: str
    supplier_name: Optional[str] = None
    depth: int                              # 1 = direct, 2 = tier-2, ...
    edge_volume_share: Optional[float] = None
    # Cumulative product of edge volume_share_pct down the path. ``None`` when
    # any edge in the path lacks a volume share.
    supplier_overall_score: float           # the persisted CompanyScore.overall_risk_score
    supplier_score_as_of_date: Optional[str] = None   # ISO date string


class ChemistryMixContribution(BaseModel):
    """Single chemistry's contribution to the company-level chemistry mix.

    Surfaced in rationale_json even though chemistry does not get its own
    pillar — the UI shows shares + each chemistry's persisted composite risk
    so users can see *why* the material pillar moved when chemistry data was
    added or refreshed.
    """

    battery_chemistry_id: int
    chemistry_slug: Optional[str] = None
    share_pct: float                            # 0–1, normalised to sum to 1.0
    composite_risk_score: Optional[float] = None  # latest ChemistryRiskScore
    score_as_of_date: Optional[str] = None        # ISO date string


class SupplierScoreRationale(BaseModel):
    inputs: ScoringInputs
    components: ComponentScores
    top_evidence: list[str]     # event IDs that most influenced the score
    decay: DecayParameters
    notes: str                  # human-readable summary for UI display
    propagation_chain: list[PropagationContribution] = []
    # Empty when the pillar was skipped (no suppliers, or scoped run).
    chemistry_mix: list[ChemistryMixContribution] = []
    # Empty when the company has no CompanyVehicleModel rows seeded.
    signals_used: dict[str, Any] = {}
    # Counts of each evidence-type the run consumed: how many scope-based regs,
    # facility-country events, propagated suppliers, weighted chemistries, etc.
    # Also surfaces ``propagation_skipped_due_to_scope`` when applicable.


# ---------------------------------------------------------------------------
# Scoped views — UI hook
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScoringScope:
    """Optional filters applied uniformly across the scoring pipeline.

    ``None`` on a field means "no filter for this dimension". ``ScoringScope.ALL``
    is the no-op sentinel; passing it MUST produce identical results to
    pre-scope behaviour (regression-tested explicitly). Scoped runs are
    *never* persisted to ``company_scores`` — the orchestrator forces
    ``persist=False`` whenever ``scope.is_all() is False``.

    Filters compose: a scope with ``material_ids={Li}`` AND
    ``country_codes={"CD"}`` means "lithium-from-DRC".
    """

    material_ids: Optional[frozenset[int]] = None
    country_codes: Optional[frozenset[str]] = None
    chemistry_ids: Optional[frozenset[int]] = None
    regulation_keys: Optional[frozenset[str]] = None
    facility_ids: Optional[frozenset[uuid.UUID]] = None
    supplier_depth_max: Optional[int] = None  # caps BFS depth in get_supplier_chain

    ALL: ClassVar["ScoringScope"]  # filled below

    def is_all(self) -> bool:
        return all(
            getattr(self, f) is None
            for f in (
                "material_ids",
                "country_codes",
                "chemistry_ids",
                "regulation_keys",
                "facility_ids",
                "supplier_depth_max",
            )
        )


ScoringScope.ALL = ScoringScope()
