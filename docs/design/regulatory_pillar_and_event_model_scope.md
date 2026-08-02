# Regulatory Pillar & Event-Model Scope
**2026-07-24. Companion to `scoring_v1_spec.md` (V1/4.1). Decisions taken with
Nicole same day; findings verified against live DB + code. Nothing here is
built yet except the items marked DONE.**

---

## 0. Decisions taken (2026-07-24)

1. **Regulation sourcing: manual-primary + filtered discovery.** The canonical
   regulation set is hand-curated behind the `verified` gate (nothing automated
   can verify). Automated feeds (Federal Register, EUR-Lex) exist ONLY as a
   relevance-filtered discovery net into a review queue. Evidence for this
   call: the unfiltered FR suggester produced 10/10 irrelevant candidates
   (archaeological-artifact import rules, Pell grants, drone exports).
2. **Noise cleanup (DONE):** those 10 `SUGGESTED_FEDERAL_REGISTER_*` rows moved
   `pending_review` → `rejected` (reversible; `verified` untouched; no score
   or display change). Review queue is now empty.
3. **Obligation model: DB-driven** (Build 2) — replace the hardcoded 8-key
   `COMPLIANCE_OBLIGATIONS` dict with model fields, so curation can add
   obligations without code edits.
4. **Event model: primary scoring category** (Build 1) — every event scores in
   exactly ONE pillar via a new `primary_category`; the multi-value
   `risk_categories_json` stays for display/filtering only.

## 1. Current state (verified 2026-07-24)

### Regulations — data is CORRECT
9 verified landmark regimes (UFLPA, EU_BATTERY_REG_2023, CRMA_2024,
IRA_DOMESTIC, EU_CSDDD, EU_CBAM, EU_REACH_COBALT, EU_CONFLICT_MINERALS, plus
SEC_CLIMATE_2024 verified-but-excluded while court-stayed). All 8 obligation
regs have `geography_compliance_weights` POPULATED — the 11.4-Reg-A comment in
`_resolve_compliance_weight` claiming they're NULL is STALE; update it in
Build 2.

### How the pillar scores
`regulatory = event_rollup (0-60, top-3 regulatory-category event impacts)
+ obligation_uplift (0-40, Σ base_points × geo_weight, capped)`. Obligations
are frozen to the 8 hardcoded keys in `regulatory_risk.py` — a curated reg can
NEVER carry obligation weight without a code edit. That's the §5 "needs data
work" flatness: the pillar differentiates only where the 8 regs' geo-weights
differ.

### Cross-pillar double-count — CONFIRMED (violates spec Principle 3)
Pillar event selection is `risk_categories_json.contains([category])`
(evidence_query.py:570) — a multi-tagged event scores in EVERY tagged pillar.
Exposure: 107 multi-tagged events (89×2, 16×3, 2×4). Regulatory overlap:
22 also-geopolitical + 20 also-operational. Systematic case: sanctions events
tagged BOTH geopolitical_trade AND regulatory_compliance — §4 files sanctions
under geopolitical. Bounded (top-3 per pillar) but real inflation.

### Event inventory by stream (3,604 total)
| Stream | n | Links | Verdict |
|---|---|---|---|
| GTA | 2,576 | mat+geo ✓ | Healthy. 1,272 are supportive/subsidy measures — deliberate (`production_subsidy_distortion`, 0.10 weight; `positive_policy_scoring.md` pending). 12 events future-dated to 2029 — check decay math handles negative age. |
| IEA policy tracker | 438 | geo ✓, mat 55% | Healthy. |
| manual_walkthrough | 207 | mat+geo ✓ | Healthy, curated. |
| SEC EDGAR | 144 | **NONE** | **Broken**: no company/material/geo links; ingester never inserts `risk_event_companies`; material attribution gated off (metadata-stub narrative); nothing consumes `sec_filing_signal`. Orphan noise. |
| (no source doc) | 137 | mixed | 119 = trade-signal builder DERIVED STATISTICS ("AU lithium exports fell 74% YoY") — arguably not "what governments have done" (§4). 18 = OpenSanctions without provenance rows. |
| Federal Register | 101 | mat 98% | Needs the relevance filter (Build 3). |
| EUR-Lex | 6 | ✓ | Dormant (6 events ever) — park or invest. |

### Parser staleness (git last-touched)
Fresh (Jul): gta, iea_policy_tracker, event_dedupe, mcs, seeds. Stale:
eurlex (05-19), comtrade/trade_signal_builder (06-05/07), opensanctions
(06-07), SEC trio (06-03..17), ingest_federal_register (06-14).

## 2. Build 1 — primary scoring category + event hygiene

- Migration: `risk_events.primary_category` (enum-valued string, NOT NULL
  after backfill). `risk_categories_json` unchanged (display/filter).
- **Assignment at ingestion, by source** (source is the reliable signal):
  GTA → geopolitical_trade (subsidy types too — they feed the subsidy input);
  OpenSanctions → geopolitical_trade; IEA tracker → per its category map;
  manual → curator-chosen (already single-category in practice);
  regulation-derived / FR → regulatory_compliance;
  operational curation → operational. Precedence fallback for ambiguous:
  operational > geopolitical > regulatory (most-specific wins).
- Backfill all ~3,600 events by the same table; the ~107 multi-tagged rows
  resolve by precedence.
- Scoring: `evidence_query` filters on `primary_category` (one-line change per
  query) — fixes the double-count for ALL pillars at once.
- Guard: ingestion validator requires primary_category ∈ enum; pipeline test.
- **Flagged decisions — RESOLVED (Nicole, 2026-07-24):**
  1. **SEC stream: pause + quarantine.** Stop the ingester schedule; exclude
     the 144 orphans from event views (no deletion). CIK→company linkage
     becomes a deliberate future build alongside company-path migration.
  2. **Trade-signal derived events (119): demote to display-only.** No
     primary_category → never selected by pillar queries; remain visible as
     signals. (Implementation: primary_category nullable, NULL = display-only;
     the NOT-NULL-after-backfill constraint drops to a validator that allows
     NULL only for the derived-signal stream.)
  3. **EUR-Lex: park.** Disable schedule, keep code; revisit after the FR
     discovery net proves itself.
- Requires full rescore. Expected movement: regulatory ↓ where sanctions
  double-counted; geopolitical ~flat (sanctions already there). Eyeball
  before/after on cobalt + REE before accepting.

## 3. Build 2 — DB-driven obligations

- Migration: `regulations.is_obligation` (bool), `obligation_points` (int,
  the base-points column), keep `geography_compliance_weights` as the per-geo
  multiplier. Backfill the 8 hardcoded values from `COMPLIANCE_OBLIGATIONS`.
- `score_regulatory_profile` + `_derive_market_regulatory_inputs` read from
  the DB; delete the dict; fix the stale 11.4-Reg-A comment.
- Material scoping continues via existing regulation material scopes.
- Same rescore as Build 1.

## 4. Build 3 — suggester relevance filter
Battery/critical-mineral relevance pre-filter on `ingest_federal_register`
(keyword + material-alias match against title/abstract; agency allowlist,
e.g. BIS/DOE/EPA/Interior over CBP-cultural-property and ED). Queue target:
< ~5 candidates/month, mostly relevant. Independent of the rescore.

## 5. Later
- Admin regulation review queue (accept/reject/verify, set obligation points,
  geo weights, material scope) — after Builds 1-2 define the fields. Ties into
  the existing admin Content section.
- Positive-policy scoring design (the 1,272 supportive GTA measures).
- Future-dated event decay clamp (verify during Build 1's rescore).

## 6. Order
Builds 1+2 together (one rescore), then 3, then the admin queue.
