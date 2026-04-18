"""Regulation reviewer.

For every seeded ``Regulation``, scan Federal Register ``SourceDocument`` rows
ingested after the regulation's ``updated_at`` (or since ``since_date`` if
supplied) and score each doc with three signals driven by
:mod:`app.services.review.regulation_registry`:

- **Signal A — FR query_name match** (+2): the ingestion pipeline tagged the
  doc with a ``query_name`` that the registry says correlates with this
  regulation. This is the strongest indicator because the ingestion step
  already bucketed the document into a topical query.
- **Signal B — keyword match** (+1 per distinct keyword, capped at +2):
  registry keywords scanned against ``title`` + ``raw_text`` (case-insensitive).
- **Signal C — agency match** (+1): any of the registry's ``agency_hints``
  appears in ``metadata_json.agencies``.

Regulations without a registry entry are skipped with a log warning. The
reviewer never mutates the seeded rows; the output is a list of
:class:`Finding` for the orchestrator to persist.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional

import structlog
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.models.documents import SourceDocument
from app.models.enums import SourceType
from app.models.regulatory import Regulation
from app.models.source import Source
from app.services.review.regulation_registry import (
    REGULATION_REVIEW_CONFIG,
    RegulationReviewConfig,
    get_config,
)
from app.services.review.scoring import as_utc_datetime, score_to_relevance
from app.services.review.types import Finding

log = structlog.get_logger(__name__)


def _score_document(
    doc: SourceDocument, config: RegulationReviewConfig
) -> tuple[int, dict[str, Any]]:
    """Score a single FR document for a regulation; return (score, signals)."""
    signals: dict[str, Any] = {}

    # Signal A: FR query_name match (+2).
    query_name = None
    metadata: dict[str, Any] = doc.metadata_json or {}
    if isinstance(metadata, dict):
        query_name = metadata.get("query_name")
    fr_query_names = config.get("fr_query_names") or []
    query_hit = bool(query_name and query_name in fr_query_names)
    signals["query_name_match"] = {
        "hit": query_hit,
        "query_name": query_name,
        "points": 2 if query_hit else 0,
    }

    # Signal B: keyword match (+1 per distinct keyword, capped +2).
    haystack_parts = [doc.title or "", doc.raw_text or ""]
    haystack = " ".join(haystack_parts).lower()
    matched_keywords: list[str] = []
    for kw in config.get("keywords") or []:
        if kw.lower() in haystack:
            matched_keywords.append(kw)
    keyword_points = min(len(matched_keywords), 2)
    signals["keyword_match"] = {
        "hit": bool(matched_keywords),
        "matched": matched_keywords,
        "points": keyword_points,
    }

    # Signal C: agency match (+1).
    agencies: list[str] = []
    if isinstance(metadata, dict):
        raw_agencies = metadata.get("agencies") or []
        if isinstance(raw_agencies, list):
            for a in raw_agencies:
                if isinstance(a, str):
                    agencies.append(a)
                elif isinstance(a, dict):
                    name = a.get("name") or a.get("raw_name")
                    if isinstance(name, str):
                        agencies.append(name)
    agency_hit_name: Optional[str] = None
    for hint in config.get("agency_hints") or []:
        hint_lower = hint.lower()
        for a in agencies:
            if hint_lower in a.lower():
                agency_hit_name = a
                break
        if agency_hit_name:
            break
    agency_points = 1 if agency_hit_name else 0
    signals["agency_match"] = {
        "hit": bool(agency_hit_name),
        "agency": agency_hit_name,
        "points": agency_points,
    }

    score = signals["query_name_match"]["points"] + keyword_points + agency_points
    signals["total_score"] = score
    return score, signals


def _candidate_documents(
    session: Session,
    regulation: Regulation,
    since: Optional[date],
) -> list[SourceDocument]:
    """Fetch candidate FR source_documents for one regulation.

    A candidate is any FEDERAL_REGISTER document whose ``published_at`` is after
    the regulation's ``updated_at`` and after ``since`` (if supplied).
    """
    cutoffs: list[datetime] = []
    reg_updated = as_utc_datetime(regulation.updated_at)
    if reg_updated is not None:
        cutoffs.append(reg_updated)
    since_dt = as_utc_datetime(since)
    if since_dt is not None:
        cutoffs.append(since_dt)

    stmt = (
        select(SourceDocument)
        .join(Source, SourceDocument.source_id == Source.id)
        .where(Source.source_type == SourceType.FEDERAL_REGISTER.value)
    )
    if cutoffs:
        cutoff = max(cutoffs)
        stmt = stmt.where(
            or_(
                SourceDocument.published_at.is_(None),
                SourceDocument.published_at > cutoff,
            )
        )
    stmt = stmt.order_by(SourceDocument.published_at.desc().nullslast())

    return list(session.execute(stmt).scalars().all())


def _evidence_json(doc: SourceDocument, signals: dict[str, Any]) -> dict[str, Any]:
    published = doc.published_at.isoformat() if doc.published_at else None
    metadata = doc.metadata_json if isinstance(doc.metadata_json, dict) else {}
    return {
        "document_id": doc.id,
        "external_id": doc.external_id,
        "title": doc.title,
        "url": doc.url,
        "published_at": published,
        "query_name": metadata.get("query_name"),
        "matched_keywords": signals.get("keyword_match", {}).get("matched", []),
        "agency_match": signals.get("agency_match", {}).get("agency"),
    }


def review(
    session: Session,
    *,
    since_date: Optional[date] = None,
    regulation_keys: Optional[Iterable[str]] = None,
) -> list[Finding]:
    """Run the regulation reviewer and return a list of findings."""
    stmt = select(Regulation)
    if regulation_keys is not None:
        keys = list(regulation_keys)
        if not keys:
            return []
        stmt = stmt.where(Regulation.regulation_key.in_(keys))
    regulations: list[Regulation] = list(session.execute(stmt).scalars().all())

    findings: list[Finding] = []
    for regulation in regulations:
        config = get_config(regulation.regulation_key)
        if config is None:
            log.warning(
                "regulation_reviewer.skip_no_config",
                regulation_key=regulation.regulation_key,
                note=(
                    "Add an entry to REGULATION_REVIEW_CONFIG "
                    "(app/services/review/regulation_registry.py)."
                ),
            )
            continue

        for doc in _candidate_documents(session, regulation, since_date):
            score, signals = _score_document(doc, config)
            if score <= 0:
                continue
            relevance = score_to_relevance(score)
            if relevance == "none":
                continue
            findings.append(
                Finding(
                    seed_type="regulation",
                    seed_key=regulation.regulation_key,
                    seed_display_name=regulation.title,
                    seed_identifier_json={
                        "regulation_id": regulation.id,
                        "regulation_key": regulation.regulation_key,
                    },
                    evidence_type="federal_register_document",
                    source_document_id=doc.id,
                    risk_event_id=None,
                    evidence_json=_evidence_json(doc, signals),
                    relevance=relevance,
                    match_signals_json=signals,
                )
            )
    return findings


__all__ = ["review", "REGULATION_REVIEW_CONFIG"]
