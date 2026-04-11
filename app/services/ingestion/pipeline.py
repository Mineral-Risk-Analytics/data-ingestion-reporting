"""
Orchestration: fetch → raw storage → `source_documents` → domain tables → `risk_events`.

Phase 1 implements federal register, census trade, SEC EDGAR, and news stub flows.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    IngestionRun,
    RawApiPayload,
    Regulation,
    RiskEvent,
    Source,
    SourceDocument,
    TradeFlow,
)
from app.models.enums import DocumentType, ImplementationPhase, IngestionStatus, SourceType
from app.services.ingestion.adapters import ADAPTER_BY_TYPE
from app.services.ingestion.adapters.news import NewsAdapter
from app.services.ingestion.base import FetchBundle
from app.services.ingestion.entity_resolution import (
    CachedCompanyInfo,
    build_company_cache,
    persist_company_links,
    resolve_companies_for_event,
)
from app.services.ingestion.normalizers.event_normalizer import (
    RiskEventDraft,
    build_news_event,
    build_regulatory_risk_event,
    build_sec_filing_event,
    build_trade_risk_event,
)
from app.services.ingestion.normalizers.material_resolver import MaterialResolver
from app.services.ingestion.normalizers.geography_resolver import GeographyResolver
from app.services.ingestion.parsers import (
    parse_article,
    parse_census_trade_rows,
    parse_federal_register_document,
    parse_sec_filing,
)
from app.services.ingestion.run_tracker import IngestionRunTracker
from app.utils.hashing import sha256_bytes

log = structlog.get_logger(__name__)
from app.utils.storage import LocalFilesystemStorage, get_local_storage


class IngestionPipeline:
    """Entry point for CLI and internal API to run ingestion."""

    def __init__(
        self,
        db: Session,
        *,
        storage: LocalFilesystemStorage | None = None,
    ) -> None:
        self._db = db
        self._storage = storage or get_local_storage()
        self._tracker = IngestionRunTracker(db)

    def run(
        self,
        source_id: int,
        *,
        params: dict[str, Any] | None = None,
    ) -> int:
        """
        Execute ingestion for a `sources.id`. Returns `ingestion_runs.id`.

        Commits the session on success; on failure persists failed run state then re-raises.
        """
        source = self._db.get(Source, source_id)
        if source is None:
            raise ValueError(f"Unknown source_id={source_id}")
        if not source.is_active:
            raise ValueError(f"Source {source_id} is inactive")

        adapter_cls = ADAPTER_BY_TYPE.get(source.source_type)
        if adapter_cls is None:
            raise ValueError(f"No adapter registered for source_type={source.source_type}")

        merged = {**(source.config_json or {}), **(params or {})}
        run = self._tracker.start(source.id, merged)
        self._db.flush()

        # Cache all companies once per run to avoid N+1 queries during entity resolution.
        company_cache: list[CachedCompanyInfo] = build_company_cache(self._db)
        # Accumulate company IDs (UUID) touched by new risk events for post-run rescoring.
        touched_company_ids: set[uuid.UUID] = set()

        try:
            if adapter_cls is NewsAdapter:
                adapter = NewsAdapter()
            else:
                adapter = adapter_cls()

            settings = get_settings()
            headers = {"User-Agent": settings.sec_edgar_user_agent}
            with httpx.Client(headers=headers, follow_redirects=True) as client:
                bundles = adapter.fetch(client, params=merged)

            batch_idx = 0
            for bundle in bundles:
                self._persist_raw_payload(run, source, bundle, batch_idx)
                batch_idx += 1
                self._tracker.add_stat(run, "items_fetched", len(bundle.items))

                if source.source_type == SourceType.FEDERAL_REGISTER.value:
                    written = self._ingest_federal_items(
                        run, source, bundle, company_cache, touched_company_ids
                    )
                elif source.source_type == SourceType.CENSUS_TRADE.value:
                    written = self._ingest_census_items(
                        run, source, bundle, merged, company_cache, touched_company_ids
                    )
                elif source.source_type == SourceType.SEC_EDGAR.value:
                    written = self._ingest_sec_items(
                        run, source, bundle, merged, company_cache, touched_company_ids
                    )
                elif source.source_type == SourceType.NEWS.value:
                    written = self._ingest_news_items(
                        run, source, bundle, company_cache, touched_company_ids
                    )
                else:
                    raise NotImplementedError(
                        f"Pipeline missing handler for {source.source_type} "
                        f"(phase {ImplementationPhase.PHASE_1.value} only)"
                    )
                self._tracker.add_stat(run, "items_written", written)

            self._tracker.complete(run, IngestionStatus.SUCCESS)

            # Rescore every company touched by new risk events.
            # A scoring failure must not roll back successfully ingested events —
            # the try/except is intentional. The next ingestion run will rescore
            # the company when it reappears in touched_company_ids.
            from app.services.scoring.orchestrator import rescore_company
            for company_id in touched_company_ids:
                try:
                    rescore_company(self._db, company_id, run_id=str(run.id))
                except Exception as score_exc:
                    log.error(
                        "pipeline.rescore_failed",
                        company_id=str(company_id),
                        run_id=run.id,
                        error=str(score_exc),
                        exc_info=True,
                    )

            self._db.commit()
            return run.id
        except Exception as exc:
            self._tracker.fail(run, exc)
            self._db.commit()
            raise

    def _persist_raw_payload(
        self,
        run: IngestionRun,
        source: Source,
        bundle: FetchBundle,
        batch_idx: int,
    ) -> None:
        rel_key = f"ingestion/run-{run.id}/batch-{batch_idx}.json"
        path = self._storage.write_bytes(rel_key, bundle.raw_body, suffix="")
        payload = RawApiPayload(
            ingestion_run_id=run.id,
            source_id=source.id,
            endpoint=bundle.endpoint[:1024],
            http_status=200,
            request_params_json=bundle.request_params,
            response_body_path=path,
            checksum=sha256_bytes(bundle.raw_body),
        )
        self._db.add(payload)

    def _ingest_federal_items(
        self,
        run: IngestionRun,
        source: Source,
        bundle: FetchBundle,
        company_cache: list[CachedCompanyInfo],
        touched_company_ids: set[uuid.UUID],
    ) -> int:
        count = 0
        for item in bundle.items:
            parsed = parse_federal_register_document(item)
            if not parsed.external_id:
                continue
            meta = {
                "document_number": parsed.document_number,
                "topics": parsed.topics,
                "agencies": parsed.agencies,
                "federal_register": parsed.raw_metadata,
            }
            doc = self._upsert_document(
                source=source,
                external_id=parsed.external_id,
                title=parsed.title,
                url=parsed.html_url or parsed.pdf_url,
                published_at=(
                    datetime.combine(parsed.publication_date, datetime.min.time()).replace(
                        tzinfo=timezone.utc
                    )
                    if parsed.publication_date
                    else None
                ),
                document_type=DocumentType.API_JSON.value,
                raw_text=parsed.abstract_text,
                metadata_json=meta,
                checksum=sha256_bytes(json.dumps(item, default=str).encode("utf-8")),
            )
            reg_key = str(parsed.document_number or parsed.external_id)
            issuing = ", ".join(parsed.agencies[:6]) if parsed.agencies else None
            self._upsert_regulation(
                doc=doc,
                regulation_key=reg_key,
                title=parsed.title,
                issuing_body=issuing,
                geography="US",
                policy_theme="; ".join(parsed.topics[:5]) if parsed.topics else None,
                status=parsed.doc_type,
                publication_date=parsed.publication_date,
                effective_date=parsed.effective_date,
                summary=parsed.abstract_text,
                extra_meta={"source_api": "federal_register"},
            )
            self._add_risk_event(
                doc, build_regulatory_risk_event(parsed), company_cache, touched_company_ids
            )
            count += 1
        _ = run  # reserved for future per-run metrics
        return count

    def _ingest_census_items(
        self,
        run: IngestionRun,
        source: Source,
        bundle: FetchBundle,
        merged: dict[str, Any],
        company_cache: list[CachedCompanyInfo],
        touched_company_ids: set[uuid.UUID],
    ) -> int:
        _ = run
        count = 0
        geo = GeographyResolver()
        materials = MaterialResolver(self._db)
        for item in bundle.items:
            table = item.get("census_table")
            if not isinstance(table, list):
                continue
            period = str(item.get("time") or merged.get("time") or "")
            ds = str(item.get("dataset_path") or merged.get("dataset_path") or "imports/hs")
            ie = "import" if "import" in ds else "export"
            rows = parse_census_trade_rows(table, import_export=ie)
            ext_id = f"census-{ds}-{period}"
            doc = self._upsert_document(
                source=source,
                external_id=ext_id,
                title=f"Census trade {ds} {period}",
                url=bundle.endpoint,
                published_at=None,
                document_type=DocumentType.TABULAR_EXPORT.value,
                raw_text=None,
                metadata_json={"period": period, "dataset_path": ds, "row_count": len(rows)},
                checksum=sha256_bytes(bundle.raw_body),
            )
            max_val = 0.0
            for row in rows:
                partner_iso = geo.partner_country_iso2(row.partner_code) or row.partner_code
                mid = materials.resolve_by_hs_code(row.hs_code)
                tf = TradeFlow(
                    source_document_id=doc.id,
                    period=row.period or period,
                    reporter_country=row.reporter_country,
                    partner_country=str(partner_iso or row.partner_code),
                    hs_code=row.hs_code,
                    hs_description=row.hs_description,
                    material_id=mid,
                    import_export_flag=row.import_export,
                    quantity=row.quantity,
                    quantity_unit=row.quantity_unit,
                    trade_value_usd=row.trade_value_usd,
                    metadata_json={
                        "partner_name": row.partner_name,
                        "census_partner_code": row.partner_code,
                    },
                )
                self._db.add(tf)
                if row.trade_value_usd and row.trade_value_usd > max_val:
                    max_val = row.trade_value_usd
                count += 1
            if rows:
                sample = max(rows, key=lambda r: r.trade_value_usd or 0)
                draft = build_trade_risk_event(
                    hs_description=sample.hs_description,
                    partner=sample.partner_name or sample.partner_code,
                    trade_value_usd=max_val or sample.trade_value_usd,
                    import_export=ie,
                )
                self._add_risk_event(doc, draft, company_cache, touched_company_ids)
        self._db.flush()
        return count

    def _ingest_sec_items(
        self,
        run: IngestionRun,
        source: Source,
        bundle: FetchBundle,
        merged: dict[str, Any],
        company_cache: list[CachedCompanyInfo],
        touched_company_ids: set[uuid.UUID],
    ) -> int:
        _ = merged, run
        count = 0
        for item in bundle.items:
            if not isinstance(item, dict):
                continue
            filings = parse_sec_filing(
                item,
                max_filings=int(merged.get("max_filings", 15)),
            )
            cik = str(item.get("cik", "")).zfill(10)
            for pf in filings:
                ext_id = pf.accession_number.replace("/", "-")
                doc = self._upsert_document(
                    source=source,
                    external_id=ext_id,
                    title=f"{pf.form} — {pf.company_name or cik}",
                    url=pf.primary_document_url,
                    published_at=pf.filed_at,
                    document_type=DocumentType.FILING.value,
                    raw_text=pf.narrative_excerpt,
                    metadata_json={
                        "cik": pf.cik,
                        "form": pf.form,
                        "ticker": pf.ticker,
                        "accession": pf.accession_number,
                    },
                    checksum=sha256_bytes(
                        f"{pf.accession_number}:{pf.form}".encode("utf-8"),
                    ),
                )
                self._add_risk_event(
                    doc,
                    build_sec_filing_event(
                        form=pf.form,
                        company=pf.company_name,
                        accession=pf.accession_number,
                        filed_at=pf.filed_at,
                        narrative=pf.narrative_excerpt,
                    ),
                    company_cache,
                    touched_company_ids,
                )
                count += 1
        self._db.flush()
        return count

    def _ingest_news_items(
        self,
        run: IngestionRun,
        source: Source,
        bundle: FetchBundle,
        company_cache: list[CachedCompanyInfo],
        touched_company_ids: set[uuid.UUID],
    ) -> int:
        _ = run
        count = 0
        for item in bundle.items:
            article = parse_article(item)
            if not article.external_id:
                continue
            doc = self._upsert_document(
                source=source,
                external_id=article.external_id,
                title=article.title,
                url=article.url,
                published_at=article.published_at,
                document_type=DocumentType.ARTICLE.value,
                raw_text=article.body_text,
                metadata_json={"source": article.source_name, **article.raw},
                checksum=sha256_bytes(json.dumps(item, default=str).encode("utf-8")),
            )
            self._add_risk_event(doc, build_news_event(article), company_cache, touched_company_ids)
            count += 1
        self._db.flush()
        return count

    def _upsert_document(
        self,
        *,
        source: Source,
        external_id: str,
        title: str | None,
        url: str | None,
        published_at: datetime | None,
        document_type: str,
        raw_text: str | None,
        metadata_json: dict | None,
        checksum: str | None,
    ) -> SourceDocument:
        stmt = select(SourceDocument).where(
            SourceDocument.source_id == source.id,
            SourceDocument.external_id == external_id,
        )
        existing = self._db.execute(stmt).scalar_one_or_none()
        if existing:
            existing.title = title
            existing.url = url
            existing.published_at = published_at
            existing.document_type = document_type
            existing.raw_text = raw_text
            existing.metadata_json = metadata_json
            existing.checksum = checksum
            existing.fetched_at = datetime.now(timezone.utc)
            self._db.add(existing)
            self._db.flush()
            return existing
        doc = SourceDocument(
            source_id=source.id,
            external_id=external_id,
            title=title,
            url=url,
            published_at=published_at,
            document_type=document_type,
            raw_text=raw_text,
            metadata_json=metadata_json,
            checksum=checksum,
        )
        self._db.add(doc)
        self._db.flush()
        return doc

    def _upsert_regulation(
        self,
        *,
        doc: SourceDocument,
        regulation_key: str,
        title: str | None,
        issuing_body: str | None,
        geography: str | None,
        policy_theme: str | None,
        status: str | None,
        publication_date: Any,
        effective_date: Any,
        summary: str | None,
        extra_meta: dict | None,
    ) -> None:
        stmt = select(Regulation).where(
            Regulation.source_document_id == doc.id,
            Regulation.regulation_key == regulation_key,
        )
        existing = self._db.execute(stmt).scalar_one_or_none()
        meta = dict(extra_meta or {})
        if existing:
            existing.title = title
            existing.issuing_body = issuing_body
            existing.geography = geography
            existing.policy_theme = policy_theme
            existing.status = status
            existing.publication_date = publication_date
            existing.effective_date = effective_date
            existing.summary = summary
            existing.metadata_json = meta
            self._db.add(existing)
            return
        self._db.add(
            Regulation(
                source_document_id=doc.id,
                regulation_key=regulation_key,
                title=title,
                issuing_body=issuing_body,
                geography=geography,
                policy_theme=policy_theme,
                status=status,
                publication_date=publication_date,
                effective_date=effective_date,
                summary=summary,
                metadata_json=meta,
            )
        )

    def _add_risk_event(
        self,
        doc: SourceDocument,
        draft: RiskEventDraft,
        company_cache: list[CachedCompanyInfo],
        touched_company_ids: set[uuid.UUID],
    ) -> RiskEvent:
        ev = RiskEvent(
            source_document_id=doc.id,
            event_type=draft.event_type,
            event_date=draft.event_date,
            title=draft.title[:1024],
            summary=draft.summary,
            severity_score=draft.severity_score,
            confidence_score=draft.confidence_score,
            risk_categories_json=draft.risk_categories,
            geography_json=draft.geography,
            metadata_json=draft.metadata,
        )
        self._db.add(ev)
        # Flush to obtain ev.id before entity resolution writes the FK.
        self._db.flush()

        matches = resolve_companies_for_event(self._db, ev, company_cache)
        persist_company_links(self._db, ev, matches)
        touched_company_ids.update(company_id for company_id, _, _ in matches)

        return ev
