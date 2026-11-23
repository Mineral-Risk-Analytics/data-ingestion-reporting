# SEC EDGAR Stream Audit — Paused

*2026-07-31, third in the per-ingester audit series (after GTA and IEA).
Decision, made by Nicole after this walkthrough: **pause the stream and
exclude it from the Phase 4 reset.***

## What it is

A dedicated ingester (May 2026 refactor) polling SEC EDGAR submissions for
a curated CIK list of battery-supply-chain filers. Companies only: US
filers and foreign private issuers with a CIK; CATL/VW-class non-filers are
out of scope by construction. One `sec_filing_signal` RiskEvent per filing,
plus a genuinely useful side effect — Company enrichment (SIC codes,
exchanges, former names, addresses) from the submissions JSON.

Its categorization is the most principled of the three audited ingesters: a
two-axis map from form type + 8-K item codes to (subtype, pillar,
severity). 8-K items are structured facts — item 1.03 *is* a bankruptcy
announcement — so unlike the GTA/IEA keyword scans there is no guessing.
`sec_subtype_map.py` remains the reference for how filing-derived distress
signals should classify when the stream revives.

## Why it is paused

Production state at audit (144 events): **78% are `FILING_INDEX`** — "a
filing happened" with no semantic content (Form 4 insider disclosures,
CERT, prospectus supplements). The distress signals the design targets
barely occur: one debt obligation, one restructuring, zero bankruptcies.
Structurally, **every event is an orphan**: `primary_category` NULL on all
144 (quarantined display-only since migration 062, which already described
the stream as paused), zero material links, zero company links, and stub
summaries because Workstream B's body-text extraction only partially
landed. Nothing in scoring, display, or triage consumes any of it.

The intended role — automated early-warning on supplier financial distress,
at a frequency no manual filing walkthrough can match — is real and not
covered by any other source. But it only materializes with three missing
pieces shipped together: an admission gate (keep distress items and annual
reports; drop index noise), company links enabled, and real body text.
That is company-path work, which the 2026-07-30 strategic pivot
deprioritized. Collecting orphan rows in the meantime is data collection
without a consumer — the pattern the triage plan exists to end.

## What the decision means concretely

No scheduled or manual `ingest-sec-edgar` runs while paused. The 144
existing events are deleted at the Phase 4 reset and **not re-ingested**.
The code stays (module and CLI carry pause notices); the suggestion
inversion is NOT applied to it — pointless work on a paused stream — and
`sec_filing_signal` remains in the listener's display-only carve-out, so
even an accidental run cannot create scoring-eligible rows. The stale
CIK_MAP entries noted earlier (Albemarle, MP Materials) are moot until
revival, at which point they must be fixed as part of the revival package.

## Revival criteria (one package, not piecemeal)

Admission filtering to high-signal forms/items; `LINK_EVENTS_TO_COMPANIES`
enabled with the CIK map corrected; Workstream B body text feeding real
summaries; events entering the standard triage flow (`pending_triage`,
suggestions, direction=n/a). Until all four are in hand, the stream stays
off.
