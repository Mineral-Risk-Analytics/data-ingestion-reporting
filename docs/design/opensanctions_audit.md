# OpenSanctions Ingester Audit — Phase 1 Complete

*2026-07-31, fifth and final entry in the per-ingester audit series.
Decisions made by Nicole in session: geography aggregates route
display_only; provenance + date-anchoring + inversion implemented in the
same pass. This closes Phase 1 of the triage plan.*

## Correction to the plan's premise

The plan carried "137 sanctions events with no provenance." The real split:
**119 of those are trade-signal derived statistics** (`GEOPOLITICAL_TRADE`,
Comtrade-derived, deliberately sourceless and display-only under migration
062's design — not this ingester's output and not in scope here). Only
**18 events come from OpenSanctions**: 7 `sanctions_listing` company
matches and 11 `geography_sanctions_exposure` country aggregates.

## What it does and how it categorizes

Downloads the free daily OpenSanctions consolidated CSV (~15–30MB) and
produces two event shapes: a company-match event when a seeded company's
LEI (exact, primary) or normalized name (fallback) matches a sanctioned
entity, and a country aggregate counting sanctioned-entity volume for
`is_sanctions_risk` geographies. Categorization is hardcoded: both
`geopolitical_trade` + `regulatory_compliance` tags, subtype
`EXPORT_RESTRICTION` (sanctions feed the export-restriction sub-input),
severity 1.0 / confidence 1.0 for company matches, `min(1, count/500)` for
aggregates. Material attribution is the most principled in the codebase —
curated `company_material_exposures` and `material_production_shares`
rather than keyword scans. True upsert on stable keys (company id /
country), so the events are fully reproducible at the reset.

## Findings and what was implemented

**F1 — No provenance chain existed at all.** No Source row, no
SourceDocument, `source_document_id` NULL on every event — the only
ingester in the corpus with zero provenance. Implemented: an
`OpenSanctions` Source and one SourceDocument per dataset snapshot date
(url → the human-readable dataset page; CSV url and entity count in
metadata). New events point at it, and the upsert path repairs NULL
`source_document_id` on the 18 historical rows at the next run — no
backfill script needed.

**F2 — Events never aged.** `event_date = now()` at insert *and* refreshed
by every upsert, so recency decay saw a years-old listing as brand-new
forever — the future-dated-events defect in another costume. Implemented:
company events anchor to the matched entities' most recent `first_seen`
(when the entity actually entered a list; snapshot date as last resort),
and geography aggregates keep their first-seen row date across upserts
with the latest snapshot recorded in metadata only.

**F3 — The 11 geography aggregates were scoring despite being
statistics.** Standing counts of sanctioned entities per country — the
same character as the quarantined trade-signal statistics, but they
carried `primary_category` and sat in geopolitical evidence pools at up to
~0.53 severity with perpetually-fresh dates. Decision: route
`display_only` (`triage_route = auto_display_only_statistic`) — visible
country-page context, not scoring evidence. The 7 company listings — real,
severity-1.0 signals — queue as `pending_triage` like other restrictive
events.

**F4 — Upserts now survive curation.** The refresh path deleted and
re-created all material links on every run, which would have silently
wiped a human-confirmed link (or resurrected a rejected one). Implemented:
only `status='suggested'` links are refreshed; confirmed and rejected
links survive, and the attribution helpers skip materials whose links
survived so the unique constraint cannot fire. Triage fields
(`triage_status`, `primary_category`) are never touched by the upsert.

**Overlap with Federal Register's OFAC stream: minor by shape.** FR
captures dated designation *actions* (documents); OpenSanctions captures
list *state* (entity currently sanctioned, across ~all jurisdictions'
lists, not just OFAC). Row-level collisions are rare; where both exist,
the FR document is the citable primary for the US action and the
OpenSanctions match is the standing-condition confirmation. No
canonicality rule needed beyond the duplicate-hints UI.

**Severity 1.0 / confidence 1.0 on company matches — left alone,
flagged.** A name-fallback match at absolute maximum severity and
certainty is aggressive (the module's own docstring warns name matching
can false-positive). Under the inversion these events queue for review
rather than scoring unreviewed, which contains the risk; the calibration
itself is a scoring-phase question, deferred like all weighting.

## Test coverage

Five new SQLite-backed tests (`tests/test_opensanctions_triage.py`)
covering snapshot provenance, first_seen anchoring, suggestion fields,
display_only routing for aggregates, curation-surviving upserts, and
NULL-provenance repair — complementing the 75 existing mock-based tests,
all of which still pass.
