"""
Build preliminary `risk_events` rows from parsed domain objects.

Scores are heuristic and explainable — replaced by richer models in later phases.
All severity_score and confidence_score values are on the 0-1.0 scale required by
the scoring formula (event_impact = severity × effective_confidence × recency × relevance).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.constants import RiskCategory
from app.services.ingestion.parsers.article_parser import ParsedArticle
from app.services.ingestion.parsers.regulation_parser import ParsedRegulation


@dataclass
class RiskEventDraft:
    event_type: str
    title: str
    summary: str | None
    event_date: datetime | None
    severity_score: float
    confidence_score: float
    risk_categories: list[str]
    geography: dict
    metadata: dict


def build_regulatory_risk_event(parsed: ParsedRegulation) -> RiskEventDraft:
    severity = 0.35
    text = (parsed.abstract_text or parsed.title or "").lower()
    keywords = ("battery", "critical mineral", "tariff", "section 232", "import")
    hits = sum(1 for k in keywords if k in text)
    severity = min(0.95, severity + hits * 0.10)
    cats = [RiskCategory.REGULATORY_COMPLIANCE.value]
    if "tariff" in text or "trade" in text:
        # Trade-related regulatory notices also carry a geopolitical dimension
        cats.append(RiskCategory.GEOPOLITICAL_TRADE.value)

    geo = {"scope": "federal", "topics": parsed.topics[:10]}

    return RiskEventDraft(
        event_type="federal_register_notice",
        title=parsed.title or parsed.document_number or "Federal Register document",
        summary=parsed.abstract_text,
        event_date=(
            datetime.combine(parsed.publication_date, datetime.min.time()).replace(tzinfo=timezone.utc)
            if parsed.publication_date
            else None
        ),
        severity_score=severity,
        confidence_score=0.70,
        risk_categories=cats,
        geography=geo,
        metadata={"agencies": parsed.agencies, "doc_type": parsed.doc_type},
    )


def build_sec_filing_event(
    *,
    form: str,
    company: str | None,
    accession: str,
    filed_at: datetime | None,
    narrative: str | None,
) -> RiskEventDraft:
    severity = 0.25
    if form.upper() in {"8-K", "6-K"}:
        severity += 0.15
    if form.upper() == "10-K":
        severity += 0.05
    text = (narrative or "").lower()
    if any(x in text for x in ("supply chain", "lithium", "battery", "sec 1502")):
        severity += 0.10

    return RiskEventDraft(
        event_type="sec_filing_signal",
        title=f"{form} — {company or 'issuer'}",
        summary=narrative[:4000] if narrative else None,
        event_date=filed_at,
        severity_score=min(0.90, severity),
        confidence_score=0.55,
        risk_categories=[
            # SEC filings map to financial_pressure; operational signals covered by other events
            RiskCategory.FINANCIAL_PRESSURE.value,
        ],
        geography={"issuer": company, "accession": accession},
        metadata={"form": form},
    )


def build_news_event(article: ParsedArticle) -> RiskEventDraft:
    severity = 0.30 + min(0.40, len(article.event_labels) * 0.05)
    cats = [RiskCategory.OPERATIONAL.value]  # default pillar for unclassified news
    if any("regulation" in e.lower() or "policy" in e.lower() for e in article.event_labels):
        cats.append(RiskCategory.REGULATORY_COMPLIANCE.value)

    return RiskEventDraft(
        event_type="news_article",
        title=article.title,
        summary=(article.body_text or "")[:4000] or None,
        event_date=article.published_at,
        severity_score=min(0.95, severity),
        confidence_score=0.45,
        risk_categories=cats,
        geography={"source": article.source_name},
        metadata={"entities": article.entities[:20], "labels": article.event_labels},
    )


def build_trade_risk_event(
    *,
    hs_description: str | None,
    partner: str | None,
    trade_value_usd: float | None,
    import_export: str,
    period: str | None = None,
) -> RiskEventDraft:
    """Build a 'trade observation' event draft from a Census trade row sample.

    ``period`` (added 2026-05-11) should be the Census ``time`` field —
    typically ``"YYYY-MM"`` (monthly) or ``"YYYY"`` (annual).  When passed,
    ``event_date`` is anchored to the end of that period so re-running
    ingestion against the same Census batch produces the same
    ``event_date`` and the pipeline's content_hash dedup matches.

    Falls back to ``datetime.now(timezone.utc)`` only when ``period`` is
    omitted or unparseable, preserving prior behavior for any legacy callers.
    """
    severity = 0.20
    if trade_value_usd and trade_value_usd > 1e9:
        severity += 0.25
    desc = (hs_description or "").lower()
    if "lithium" in desc or "battery" in desc:
        severity += 0.20

    event_date = _period_to_event_date(period)

    return RiskEventDraft(
        event_type="trade_flow_signal",
        title=f"Trade observation ({import_export}) — {partner or 'partner'}",
        summary=hs_description,
        event_date=event_date,
        severity_score=min(0.90, severity),
        confidence_score=0.50,
        risk_categories=[RiskCategory.GEOPOLITICAL_TRADE.value],
        geography={"partner": partner},
        metadata={"trade_value_usd": trade_value_usd, "period": period},
    )


def _period_to_event_date(period: str | None) -> datetime:
    """Anchor a Census trade event to the end of its reporting period.

    Accepts ``"YYYY-MM"``, ``"YYYYMM"``, and ``"YYYY"`` formats — the three
    period shapes Census and Comtrade emit.  Returns ``datetime.now(UTC)``
    on parse failure so legacy callers without a period argument still
    work (the pipeline dedup just falls back to per-run uniqueness).
    """
    if not period:
        return datetime.now(timezone.utc)
    p = period.strip()
    try:
        if len(p) == 7 and p[4] == "-":
            from calendar import monthrange
            year = int(p[:4])
            month = int(p[5:7])
            last_day = monthrange(year, month)[1]
            return datetime(year, month, last_day, tzinfo=timezone.utc)
        if len(p) == 6 and p.isdigit():
            from calendar import monthrange
            year = int(p[:4])
            month = int(p[4:6])
            last_day = monthrange(year, month)[1]
            return datetime(year, month, last_day, tzinfo=timezone.utc)
        if len(p) == 4 and p.isdigit():
            return datetime(int(p), 12, 31, tzinfo=timezone.utc)
    except (ValueError, IndexError):
        pass
    return datetime.now(timezone.utc)
