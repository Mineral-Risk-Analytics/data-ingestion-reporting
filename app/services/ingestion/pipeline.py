"""
Orchestration: fetch → raw storage → `source_documents` → domain tables → `risk_events`.

Phase 1 implements federal register, census trade, SEC EDGAR, and news stub flows.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

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
    RiskEventHsMapping,
    RiskEventMaterial,
    Source,
    SourceDocument,
    TradeFlow,
)
from app.models.enums import DocumentType, ImplementationPhase, IngestionStatus, SourceType
from app.services.ingestion.adapters import ADAPTER_BY_TYPE
from app.services.ingestion.adapters.news import NewsAdapter
from app.services.ingestion.base import FetchBundle
from app.services.ingestion import feature_flags
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
from app.utils.hashing import sha256_bytes, sha256_text

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

        # Cache all companies once per run to avoid N+1 queries during entity
        # resolution.  Gated by feature_flags.LINK_EVENTS_TO_COMPANIES — when
        # the flag is False (Foundation phase 3 default), skip the cache build
        # entirely.  ``company_cache`` becomes None and the per-event
        # _link_companies path short-circuits accordingly.
        company_cache: Optional[list[CachedCompanyInfo]] = (
            build_company_cache(self._db)
            if feature_flags.LINK_EVENTS_TO_COMPANIES
            else None
        )

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
                        run, source, bundle, company_cache
                    )
                elif source.source_type == SourceType.CENSUS_TRADE.value:
                    written = self._ingest_census_items(
                        run, source, bundle, merged, company_cache
                    )
                elif source.source_type == SourceType.SEC_EDGAR.value:
                    written = self._ingest_sec_items(
                        run, source, bundle, merged, company_cache
                    )
                elif source.source_type == SourceType.NEWS.value:
                    written = self._ingest_news_items(
                        run, source, bundle, company_cache
                    )
                else:
                    raise NotImplementedError(
                        f"Pipeline missing handler for {source.source_type} "
                        f"(phase {ImplementationPhase.PHASE_1.value} only)"
                    )
                self._tracker.add_stat(run, "items_written", written)

            self._tracker.complete(run, IngestionStatus.SUCCESS)
            # Rescoring is handled by the scheduled Inngest cron (scoring_jobs.py),
            # not triggered inline here. See docs/ingestion-pipeline.md §Phase 3 decoupling.
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
                # ``reg_key`` here is a Federal Register document number
                # (e.g. "2024-12345"), not a canonical regulation_key.  The
                # resolver looks it up under source_system='federal_register'
                # in regulation_aliases.  Unknown FR doc numbers are skipped
                # with warning rather than auto-creating a regulation row.
                source_system="federal_register",
            )
            self._add_risk_event(
                doc, build_regulatory_risk_event(parsed), company_cache
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
    ) -> int:
        """Census-trade pipeline branch — PARKED SCAFFOLD.

        Paired with the parked ``CensusTradeAdapter``; reachable only if
        the adapter were unparked AND ``bdi-ingest ingest census-trade``
        were re-added to the CLI mapping AND a scheduled job were wired
        up.  Today none of those are true, so this method is dead code
        in practice — preserved as a Phase 2 starting point.

        Phase 2 todo when wiring this up:

          * The synthetic ``build_trade_risk_event`` event derived from
            the single max-trade-value row is shallow.  Replace with a
            concentration / drop signal computed over the full bundle
            once US-bilateral data is flowing.
          * Confirm ``materials.resolve_by_hs_code`` handles 10-digit
            HTS codes (Census's native resolution) or truncate to
            6-digit at parse time.
          * Document the ``CTY_CODE -> ISO2`` mapping path; Census uses
            its own country code scheme (not ISO numeric, not Comtrade
            numeric).

        See ``app/services/ingestion/adapters/census_trade.py`` module
        docstring for the full Phase 2 unpark checklist.
        """
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
            # N3 audit fix (2026-05-06): track resolved (material_id,
            # hs_mapping_id) per row so the sample-derived RiskEvent below
            # can be tagged with the correct material/HS junction rows
            # instead of dropping that attribution on the floor.
            resolved_by_row: dict[int, tuple[int | None, int | None, float | None]] = {}
            for row in rows:
                partner_iso = geo.partner_country_iso2(row.partner_code) or row.partner_code
                mid, hs_mapping_id, mapping_conf = materials.resolve_by_hs_code(row.hs_code)
                resolved_by_row[id(row)] = (mid, hs_mapping_id, mapping_conf)
                tf = TradeFlow(
                    source_document_id=doc.id,
                    period=row.period or period,
                    reporter_country=row.reporter_country,
                    partner_country=str(partner_iso or row.partner_code),
                    hs_code=row.hs_code,
                    hs_description=row.hs_description,
                    material_id=mid,
                    hs_mapping_id=hs_mapping_id,
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
                sample_mid, sample_hs_id, sample_conf = resolved_by_row.get(
                    id(sample), (None, None, None)
                )
                draft = build_trade_risk_event(
                    hs_description=sample.hs_description,
                    partner=sample.partner_name or sample.partner_code,
                    trade_value_usd=max_val or sample.trade_value_usd,
                    import_export=ie,
                    # Anchor the event to the Census reporting period so
                    # re-running against the same payload reuses the same
                    # content_hash via pipeline._add_risk_event dedup.
                    # Without this, event_date=now() would shift each run
                    # and pipeline._add_risk_event would silently insert
                    # a duplicate.  See 2026-05-11 audit.
                    period=sample.period or period,
                )
                self._add_risk_event(
                    doc,
                    draft,
                    company_cache,
                    material_id=sample_mid,
                    hs_mapping_id=sample_hs_id,
                    mapping_confidence=sample_conf,
                )
        self._db.flush()
        return count

    def _ingest_sec_items(
        self,
        run: IngestionRun,
        source: Source,
        bundle: FetchBundle,
        merged: dict[str, Any],
        company_cache: list[CachedCompanyInfo],
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
            self._add_risk_event(doc, build_news_event(article), company_cache)
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
        source_system: str | None = None,
    ) -> None:
        """Refresh mutable fields on an existing curated regulation.

        Locked-down semantics (May 2026 refactor): this method NEVER
        creates new regulation rows.  Pipeline ingesters (Federal Register,
        SEC EDGAR, news, Census trade) surface arbitrary external IDs that
        often don't correspond to tracked regulations — silently auto-
        creating a row for every such ID poisoned the regulations table
        with partial / mis-classified records.

        Resolution order:
          1. If ``source_system`` is provided, try
             ``RegulationAliasResolver.resolve(source_system, regulation_key)``.
             This handles the "regulation_key is actually an external ID"
             case (e.g. Federal Register doc number → canonical key).
          2. Else, try a direct lookup on ``Regulation.regulation_key``.
          3. If neither resolves: log a warning and SKIP.  The caller's
             ``RiskEvent`` row still gets written but without a
             ``RiskEventRegulation`` junction, which is correct — events
             whose regulatory context we can't verify shouldn't influence
             scoring against fabricated regulation rows.

        On a successful resolve, this method updates only the *mutable*
        side of the regulation:
          * ``summary`` if the existing one is NULL (never overwrites
            curated text)
          * ``metadata_json.last_seen`` (freshness signal)
          * ``source_document_id`` if not yet set
        Curated fields (title, geography, policy_theme, status,
        publication_date, effective_date) are NOT touched — those come
        from ``seed_regulations.py``.
        """
        from datetime import datetime, timezone
        from app.services.ingestion.regulation_resolver import (
            RegulationAliasResolver,
        )

        regulation: Regulation | None = None

        # Step 1 — alias resolver (when caller provided source context).
        if source_system:
            resolver = RegulationAliasResolver(self._db)
            result = resolver.resolve(source_system, regulation_key)
            if result.status == "skipped":
                # Partner-curated decision not to track — skip silently.
                return
            if result.status == "ok":
                regulation = result.regulation

        # Step 2 — direct lookup (caller passed a canonical regulation_key).
        if regulation is None:
            regulation = self._db.scalar(
                select(Regulation).where(
                    Regulation.regulation_key == regulation_key
                )
            )

        # Step 3 — skip with warning when nothing matches.
        if regulation is None:
            log.warning(
                "pipeline.unknown_regulation",
                regulation_key=regulation_key,
                source_system=source_system,
                hint=(
                    "external regulation ID not in regulation_aliases. "
                    "Add it to seed_regulation_aliases.py (and the "
                    "regulation itself to seed_regulations.py if it's "
                    "battery-supply-chain-relevant), then re-run "
                    "seed-regulations + seed-regulation-aliases. "
                    "Event will still be written but without a "
                    "RiskEventRegulation junction."
                ),
            )
            return

        # ── Mutable-field refresh on the resolved regulation ───────────
        # Backfill summary only when missing (don't overwrite curated text).
        if summary and not regulation.summary:
            regulation.summary = summary

        # Attach SourceDocument if not yet linked (helps trace provenance).
        if regulation.source_document_id is None:
            regulation.source_document_id = doc.id

        # Bump last_seen + merge any extra metadata the caller provided.
        meta = dict(regulation.metadata_json or {})
        meta["last_seen"] = datetime.now(timezone.utc).isoformat()
        if extra_meta:
            for k, v in extra_meta.items():
                # Don't overwrite curated keys (e.g. celex, seed_version).
                meta.setdefault(k, v)
        regulation.metadata_json = meta

    def _add_risk_event(
        self,
        doc: SourceDocument,
        draft: RiskEventDraft,
        company_cache: Optional[list[CachedCompanyInfo]],
        *,
        material_id: int | None = None,
        hs_mapping_id: int | None = None,
        mapping_confidence: float | None = None,
    ) -> RiskEvent:
        """Persist a RiskEvent + entity-resolution links.

        Optional ``material_id`` / ``hs_mapping_id`` kwargs (added 2026-05-06
        as the N3 audit fix) write ``RiskEventMaterial`` and
        ``RiskEventHsMapping`` junction rows when supplied.  Used by the
        Census-trade ingestion path which already resolves these IDs at
        TradeFlow construction time — the resolved values used to fall on
        the floor when the sample-derived RiskEvent was created.

        ``mapping_confidence`` (added 2026-05-09, Tier 1.4 audit) is the
        partner-curated confidence on the ``hs_code_material_mappings``
        row used to resolve the attribution.  When supplied, the resulting
        ``RiskEventMaterial.relevance_score`` and
        ``RiskEventHsMapping.relevance_score`` are multiplied by this value
        so that a low-confidence mapping (e.g. REE → HS 2617 at 0.5
        because bastnasite/monazite share the prefix with non-REE ores)
        produces a low-relevance attribution downstream filters can drop.
        ``None`` preserves the legacy ``relevance_score=1.0`` for
        backwards compatibility.

        Without these kwargs the function preserves its prior behaviour
        (no junction writes), so news / SEC EDGAR call sites are unchanged.
        Closing those paths' attribution requires running ``MaterialCache``
        over filing / article text — tracked separately, see audit follow-up
        task ("SEC + news pipeline material attribution").

        Idempotency (added 2026-05-09):
          A ``content_hash`` derived from ``(title, summary, event_date)``
          is now computed before insert and used as a dedup key.  If an
          existing event with the same hash is found, this function
          returns it unmodified — no new row, no duplicate junction
          writes (they'd violate the ``uq_risk_event_material`` /
          ``uq_risk_event_hs_mapping`` constraints anyway).  Census trade
          re-ingest, which previously produced a fresh duplicate row on
          every run, is now properly idempotent.
        """
        title = (draft.title or "")[:1024]
        summary_str = draft.summary or ""
        date_str = (
            draft.event_date.isoformat() if draft.event_date is not None else ""
        )
        # The same shape used by gta.py / opensanctions.py / trade_signal_builder.
        content_hash = sha256_text(f"{title}|{summary_str}|{date_str}")

        existing = self._db.scalar(
            select(RiskEvent).where(RiskEvent.content_hash == content_hash).limit(1)
        )
        if existing is not None:
            # Re-running on top of existing data — return the prior event
            # without re-adding junctions.  The unique constraints on
            # (risk_event_id, material_id) and (risk_event_id, hs_mapping_id)
            # would reject duplicates anyway; bailing here is the cleaner
            # path and avoids an integrity error tickling the session.
            log.debug(
                "pipeline.add_risk_event.skip_existing",
                event_id=existing.id,
                content_hash=content_hash[:16],
            )
            return existing

        ev = RiskEvent(
            source_document_id=doc.id,
            event_type=draft.event_type,
            event_date=draft.event_date,
            title=title,
            summary=draft.summary,
            severity_score=draft.severity_score,
            confidence_score=draft.confidence_score,
            risk_categories_json=draft.risk_categories,
            geography_json=draft.geography,
            metadata_json=draft.metadata,
            content_hash=content_hash,
        )
        self._db.add(ev)
        # Flush to obtain ev.id before junction-row FKs.
        self._db.flush()

        # Confidence-weighted relevance — clamped to [0, 1] for safety
        # even though ``hs_code_material_mappings.confidence`` is already
        # constrained at insert time.
        effective_relevance = (
            1.0 if mapping_confidence is None
            else max(0.0, min(1.0, float(mapping_confidence)))
        )

        if material_id is not None:
            self._db.add(
                RiskEventMaterial(
                    risk_event_id=ev.id,
                    material_id=material_id,
                    relevance_score=effective_relevance,
                    match_reason="hs_code",
                )
            )
        if hs_mapping_id is not None:
            self._db.add(
                RiskEventHsMapping(
                    risk_event_id=ev.id,
                    hs_mapping_id=hs_mapping_id,
                    relevance_score=effective_relevance,
                    match_reason="trade_flow_match",
                )
            )

        # Gated by feature_flags.LINK_EVENTS_TO_COMPANIES (default False —
        # Foundation phase 3).  When the flag is off, ``company_cache`` is
        # None and we skip the resolver loop entirely rather than running
        # it and discarding the result inside persist_company_links.
        if feature_flags.LINK_EVENTS_TO_COMPANIES and company_cache is not None:
            matches = resolve_companies_for_event(self._db, ev, company_cache)
            persist_company_links(self._db, ev, matches)

        return ev
