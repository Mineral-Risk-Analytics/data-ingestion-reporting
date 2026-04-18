"""Shared types for seed-staleness reviewers.

A ``Finding`` is the reviewer-agnostic shape the orchestrator persists into
``seed_review_findings``. Every reviewer returns a list of these.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.services.review.scoring import Relevance


@dataclass(slots=True)
class Finding:
    """One candidate emitted by a reviewer.

    Attributes
    ----------
    seed_type
        One of: ``regulation``, ``company``, ``material_exposure``,
        ``supply_relationship``, ``facility``.
    seed_key
        Human-readable subject identifier (e.g. ``"IRA_DOMESTIC"`` or
        ``"CATL|Lithium"``). Used for display and grouping.
    seed_display_name
        Optional richer label for UI display.
    seed_identifier_json
        Typed component dict (e.g. ``{"regulation_id": 3, "regulation_key": "IRA_DOMESTIC"}``).
        Used for polymorphic UI filters without per-type FK columns.
    evidence_type
        One of: ``federal_register_document``, ``risk_event_company_link``,
        ``risk_event_material_link``, ``sec_filing_mention``, ``news_mention``,
        ``facility_text_mention``.
    source_document_id / risk_event_id
        Optional FKs to the evidence row.
    evidence_json
        Freeform shape for the UI (e.g. title, URL, agency, matched keywords).
    relevance
        Bucketed score produced via :func:`scoring.score_to_relevance`.
    match_signals_json
        Raw per-signal scores, useful for debugging and transparency.
    """

    seed_type: str
    seed_key: str
    seed_identifier_json: dict[str, Any]
    evidence_type: str
    evidence_json: dict[str, Any]
    relevance: Relevance
    match_signals_json: dict[str, Any]
    seed_display_name: Optional[str] = None
    source_document_id: Optional[int] = None
    risk_event_id: Optional[int] = None
    extra: dict[str, Any] = field(default_factory=dict)
