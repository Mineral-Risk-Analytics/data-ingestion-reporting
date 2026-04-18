"""Supply-relationship reviewer.

For every ``CompanySupplyRelationship``, scan SEC-EDGAR and news
``SourceDocument`` rows published after the relationship's ``updated_at`` and
look for two-party text mentions. The signal is deliberately weak-to-medium:
a co-occurrence in a filing or news article may mean anything from "contract
signed" to "contract terminated", so the reviewer surfaces the document and
lets a human read it.

Matching details
----------------
- Candidate documents: ``Source.source_type`` in {``sec_edgar``, ``news``};
  ``SourceDocument.published_at`` > ``relationship.updated_at`` (or
  ``since_date`` if supplied).
- Buyer / supplier name set: each company's ``canonical_name`` plus every
  alias from ``CompanyAlias`` (``alias_type`` ∈ {``aka``, ``former_name``,
  ``abbreviation``, ``ticker``}). Case-insensitive substring match against
  ``raw_text``.
- A finding is emitted only when **both** buyer and supplier have at least
  one alias hit in the same document.

Signals
-------
- **Signal A — both parties match** (+2): hard gate; recorded for provenance.
- **Signal B — SEC filing** (+1): ``document_type='filing'`` or source type
  ``sec_edgar`` carries more weight than news.
- **Signal C — contract keyword** (+1): doc text contains any of
  ``supply agreement``, ``offtake``, ``terminated``, ``expanded``, ``MOU``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyAlias, CompanySupplyRelationship
from app.models.documents import SourceDocument
from app.models.enums import SourceType
from app.models.source import Source
from app.models.supply import Material
from app.services.review.scoring import as_utc_datetime, score_to_relevance
from app.services.review.types import Finding


_ALIAS_TYPES_FOR_MATCHING = (
    "aka",
    "former_name",
    "abbreviation",
    "ticker",
)

_CONTRACT_KEYWORDS = (
    "supply agreement",
    "offtake",
    "terminated",
    "expanded",
    "mou",
)


def _party_alias_set(session: Session, company: Company) -> list[str]:
    """Build a normalized alias list (lowercase) for text matching."""
    names: set[str] = set()
    if company.canonical_name:
        names.add(company.canonical_name.strip())
    if company.legal_name:
        names.add(company.legal_name.strip())

    alias_stmt = select(CompanyAlias).where(
        CompanyAlias.company_id == company.id,
        CompanyAlias.alias_type.in_(_ALIAS_TYPES_FOR_MATCHING),
    )
    for alias_row in session.execute(alias_stmt).scalars().all():
        if alias_row.alias:
            names.add(alias_row.alias.strip())

    return [n.lower() for n in names if len(n) >= 3]


def _find_hits(haystack_lower: str, aliases: list[str]) -> list[str]:
    hits: list[str] = []
    for alias in aliases:
        if alias in haystack_lower and alias not in hits:
            hits.append(alias)
    return hits


def _score_document(
    doc: SourceDocument,
    source: Source,
    buyer_hits: list[str],
    supplier_hits: list[str],
    haystack_lower: str,
) -> tuple[int, dict[str, Any]]:
    signals: dict[str, Any] = {
        "both_parties_match": {
            "buyer_hits": buyer_hits,
            "supplier_hits": supplier_hits,
            "points": 2,
        }
    }

    is_sec = (
        doc.document_type == "filing"
        or source.source_type == SourceType.SEC_EDGAR.value
    )
    sec_points = 1 if is_sec else 0
    signals["sec_filing"] = {
        "document_type": doc.document_type,
        "source_type": source.source_type,
        "hit": is_sec,
        "points": sec_points,
    }

    matched_keywords = [kw for kw in _CONTRACT_KEYWORDS if kw in haystack_lower]
    kw_points = 1 if matched_keywords else 0
    signals["contract_keyword"] = {
        "matched": matched_keywords,
        "points": kw_points,
    }

    total = 2 + sec_points + kw_points
    signals["total_score"] = total
    return total, signals


def _evidence_json(
    doc: SourceDocument,
    source: Source,
    buyer: Company,
    supplier: Company,
    buyer_hits: list[str],
    supplier_hits: list[str],
) -> dict[str, Any]:
    published = doc.published_at.isoformat() if doc.published_at else None
    return {
        "document_id": doc.id,
        "source_type": source.source_type,
        "external_id": doc.external_id,
        "title": doc.title,
        "url": doc.url,
        "published_at": published,
        "document_type": doc.document_type,
        "buyer_alias_hits": buyer_hits,
        "supplier_alias_hits": supplier_hits,
        "buyer_canonical_name": buyer.canonical_name,
        "supplier_canonical_name": supplier.canonical_name,
    }


def review(
    session: Session,
    *,
    since_date: Optional[date] = None,
    relationship_ids: Optional[Iterable[int]] = None,
) -> list[Finding]:
    """Run the supply-relationship reviewer and return a list of findings."""
    stmt = select(CompanySupplyRelationship)
    if relationship_ids is not None:
        ids = list(relationship_ids)
        if not ids:
            return []
        stmt = stmt.where(CompanySupplyRelationship.id.in_(ids))
    relationships = list(session.execute(stmt).scalars().all())

    # Candidate documents (shared filter per-relationship based on updated_at).
    candidate_source_types = (
        SourceType.SEC_EDGAR.value,
        SourceType.NEWS.value,
    )

    findings: list[Finding] = []
    company_cache: dict[str, Company] = {}
    alias_cache: dict[str, list[str]] = {}

    def _get_company(company_id) -> Optional[Company]:
        key = str(company_id)
        if key not in company_cache:
            company = session.get(Company, company_id)
            if company is not None:
                company_cache[key] = company
        return company_cache.get(key)

    def _get_aliases(company: Company) -> list[str]:
        key = str(company.id)
        if key not in alias_cache:
            alias_cache[key] = _party_alias_set(session, company)
        return alias_cache[key]

    since_dt = as_utc_datetime(since_date)
    for rel in relationships:
        buyer = _get_company(rel.buyer_id)
        supplier = _get_company(rel.supplier_id)
        if buyer is None or supplier is None:
            continue

        cutoffs: list[datetime] = []
        rel_updated = as_utc_datetime(rel.updated_at)
        if rel_updated is not None:
            cutoffs.append(rel_updated)
        if since_dt is not None:
            cutoffs.append(since_dt)

        doc_stmt = (
            select(SourceDocument, Source)
            .join(Source, SourceDocument.source_id == Source.id)
            .where(Source.source_type.in_(candidate_source_types))
        )
        if cutoffs:
            cutoff = max(cutoffs)
            doc_stmt = doc_stmt.where(
                or_(
                    SourceDocument.published_at.is_(None),
                    SourceDocument.published_at > cutoff,
                )
            )
        doc_stmt = doc_stmt.order_by(SourceDocument.published_at.desc().nullslast())

        buyer_aliases = _get_aliases(buyer)
        supplier_aliases = _get_aliases(supplier)
        if not buyer_aliases or not supplier_aliases:
            continue

        material_name: Optional[str] = None
        if rel.material_id is not None:
            material = session.get(Material, rel.material_id)
            if material is not None:
                material_name = material.canonical_name
        seed_key_parts = [buyer.canonical_name, supplier.canonical_name]
        if material_name:
            seed_key_parts.append(material_name)
        seed_key = "|".join(seed_key_parts)

        for doc, source in session.execute(doc_stmt).all():
            haystack_parts = [doc.title or "", doc.raw_text or ""]
            haystack_lower = " ".join(haystack_parts).lower()
            if not haystack_lower.strip():
                continue

            buyer_hits = _find_hits(haystack_lower, buyer_aliases)
            if not buyer_hits:
                continue
            supplier_hits = _find_hits(haystack_lower, supplier_aliases)
            if not supplier_hits:
                continue

            score, signals = _score_document(
                doc, source, buyer_hits, supplier_hits, haystack_lower
            )
            relevance = score_to_relevance(score)
            if relevance == "none":
                continue

            evidence_type = (
                "sec_filing_mention"
                if source.source_type == SourceType.SEC_EDGAR.value
                else "news_mention"
            )

            findings.append(
                Finding(
                    seed_type="supply_relationship",
                    seed_key=seed_key,
                    seed_display_name=(
                        f"{buyer.canonical_name} ← {supplier.canonical_name}"
                        + (f" ({material_name})" if material_name else "")
                    ),
                    seed_identifier_json={
                        "relationship_id": rel.id,
                        "buyer_id": str(rel.buyer_id),
                        "supplier_id": str(rel.supplier_id),
                        "material_id": rel.material_id,
                    },
                    evidence_type=evidence_type,
                    source_document_id=doc.id,
                    risk_event_id=None,
                    evidence_json=_evidence_json(
                        doc, source, buyer, supplier, buyer_hits, supplier_hits
                    ),
                    relevance=relevance,
                    match_signals_json=signals,
                )
            )

    return findings


__all__ = ["review"]
