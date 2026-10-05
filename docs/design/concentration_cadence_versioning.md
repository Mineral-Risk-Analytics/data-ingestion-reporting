# A5 — Concentration Scoring: Data Cadence & Versioning Statement

_Drafted 2026-08-16 (Workstream A item A5, per
`concentration_first_scoring_plan.md` §2). Companion to the A6 public
methodology page, which restates the user-facing parts of this in public
language. This is the internal statement of record: what refreshes when,
what "fresh" means, which staleness we accept and why, and what a version
means. Sign-off closes A5._

## 1. The principle (stated, not apologized for)

The published score is **structural supply risk**: a pure function of
per-stage production-share tables and governance percentiles at an as-of
date. It updates when its _sources_ publish, not when news breaks — annual
cadence by construction. This is accepted by design (Nicole, 2026-08-04:
demotion decision; "even if it's delayed by time"). The live half of the
product is the event feed and the C2 live-signal indicator, not the score.
Every published score carries its `as_of` date and per-stage source +
`reference_year`, so a reader can always see exactly how current the
number is.

## 2. Sources and their refresh calendar

| Source                                                                                 | What it feeds                                                                                                                                          | Cadence                  | Expected publication                                           | Action on release                                                                                                                                   |
| -------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------ | -------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------- |
| **USGS Mineral Commodity Summaries**                                                   | Ore-stage shares, all materials                                                                                                                        | Annual                   | Late Jan / Feb (edition N carries N-1 estimates + revised N-2) | `ingest-usgs --force` re-ingest (data-year stamping + old-convention cleanup are built into the command); verify vs PDF spot-checks; rescore + diff |
| **IEA Global Critical Minerals Outlook** (CC BY 4.0)                                   | Downstream stages via workbook: Li chemicals, Ni refining, Co mining, battery-grade graphite, magnet-REE refining, Ni sulphate (prose), REE separation | Annual                   | ~May                                                           | Workbook batch: new rows at the new data year (old vintage retained as history), audit flip, `seed-benchmark-shares --force`                        |
| **Cobalt Institute Cobalt Market Report** (authored by Benchmark Mineral Intelligence) | Cobalt refined (Table 1 country split), intermediate, product-mix context                                                                              | Annual                   | ~May (edition N covers year N-1)                               | Same workbook batch path as GCMO; check each edition for a sulfate country split (see §5)                                                           |
| **World Bank WGI**                                                                     | Governance amplifier percentiles                                                                                                                       | Annual                   | ~Sept                                                          | `ingest-worldbank-wgi`; amplifier picks the new vintage up on next rescore. Current vintage: 2024                                                   |
| **`benchmark_shares_v3.xlsx`**                                                         | The single curation surface for all non-USGS shares                                                                                                    | On source releases above | —                                                              | Rows load only with `audited == "Y"` (human-verified against the cited source). One workbook, forever — no versioned siblings                       |

Practical annual rhythm: **Q1** MCS re-ingest (ore stages) → **Q2**
GCMO + CI batch (downstream stages) → **Q3/Q4** WGI vintage. Each step ends
with a manual rescore and a before/after diff review — scheduled scoring
stays off until the concentration-first cutover, and after cutover any
automated run must follow the same diff discipline.

## 3. Data-year and freshness rules (the ones that bit us)

- **`reference_year` is the DATA year, never the edition year.** MCS 2026
  rows carry 2024/2025; GCMO 2026 annex tables carry 2025; CI Report 2025
  (published May 2026) carries 2025. The USGS ingest defect that stamped
  edition years (fixed 2026-08-05) made freshness one year optimistic —
  this convention is the guard.
- **Freshness gate: 24 months.** A stage's share row qualifies as a
  scoring input only while `reference_year` is within 24 months of the
  as-of date. Older rows go display-only with an on-page stale banner and
  are excluded from the stage max.
- **The current cliff: 2028-01-01.** Every launch-list binding stage is at
  2025 data, which stays scoring-eligible through 2027-12-31. If a 2027
  source cycle were skipped entirely, all launch scores would degrade at
  once on 2028-01-01 — the annual refresh in §2 is therefore _mandatory
  maintenance_, not an improvement program. (2024-data history rows hit
  their cliff 2027-01-01, which is harmless: they're superseded history.)
- **Single-vintage within a stage.** A stage snapshot is computed from one
  reference year (latest fresh). Different _stages_ of one material may
  legitimately sit at different years; the per-stage year is displayed.
- **Absent stages contribute zero silently.** The stage-max can only see
  stages that have data. This is the invisible-understatement failure mode
  (Ni sulphate +10 pts, REE refined +3.5 pts when filled, 2026-08-11) —
  the stage-ladder coverage matrix
  (`stage_ladder_coverage_memo.md`) is re-checked as part of each annual
  refresh, not just once.

## 4. Basis and denominator conventions

- **World totals include the unattributed remainder** (Other countries /
  RoW). The remainder gets no share row and no HHI term (treated as
  atomistic). Shares on one node+year may sum below 1.0; loader tolerance
  caps the sum at 1.05.
- **Basis is recorded per row and never mixed within a stage**: production
  vs capacity vs proxy are distinct bases. Known caveats carried in row
  notes: GCMO nickel "refining" excludes intermediates (matte/MHP are
  captured at the mine and refined endpoints instead — INSG is the future
  fill path); CI cobalt figures are Benchmark-authored **sold-supply**
  data, so inventory draw/build can shift shares independent of
  production; capacity rows (e.g. REE separation) are flagged and used
  only where no production source exists.
- **The loader never invents numbers.** Rows exist only where a citable
  source states or arithmetically implies them (country ÷ world from the
  same table). Residual splits estimated from source prose are flagged as
  estimates in notes and carry `audited` review like everything else.

## 5. Accepted staleness — cobalt battery-grade sulfate (the only one)

CN 0.85 @2022 (USGS-authored Springer paper) is the corpus's only stale
row on the launch list. The CI Cobalt Market Report 2025 was checked
end-to-end (2026-08-11): it gives global sulfate volume (90 kt) and the
product split (sulphate 38 / tetroxide 32 / metal 30) but **no country
split**, and no other citable free source has one.

**Decision: accept, with mitigations, rather than estimate.**
Fabricating or deriving a share the source doesn't state violates the
loader rule (§4). Mitigations in place: cobalt's published score binds on
the fresh refined stage (CN, 2025); the detail page banners the excluded
stale stage; the freshness audit flags cobalt `understated`; the band
placement note (Option B ledger) records that the boundary reading is
conservative for exactly this reason. **Revisit trigger:** every CI/GCMO
edition — the first one that publishes a sulfate country split supersedes
the 2022 row in that cycle's batch. Fallback if it never appears: a
partner-sourced estimate loaded as an explicitly-flagged estimate row —
a decision for Nicole + partner, not a default.

## 6. Supersession and history

- **New year → new rows.** A source's new edition inserts rows at the new
  `reference_year`; the old vintage is retained as history (the loader
  shadow-warns, by design). History rows are what make trend lines and
  audit diffs possible — never delete them for tidiness.
- **Cross-source supersession at the SAME year is manual and recorded.**
  The loader has no supersession concept; replacing source A's rows with
  source B's at one node+year requires a deliberate SQL delete, recorded
  with rationale. Precedent: IEA GCMO refined-cobalt 2025 rows removed
  2026-08-11 in favor of CI Table 1 (8-country split beats 6-country;
  CI/Benchmark is the specialist source for cobalt). Default preference
  order where sources overlap: specialist benchmark report > IEA GCMO >
  USGS, decided per case and written down.
- **Criticality signals stay edition-stamped** (e.g. "USGS MCS 2026") —
  they are commentary on an edition, not a time series, and are refreshed
  by replacement.

## 7. What a version means

- **A score is a pure function of (evidence in DB, as-of date).** Every
  run is a full recompute; there is no incremental mode. Score rows are
  kept per as-of date — the sequence of runs is the trend line, and any
  two runs diff cleanly.
- **`SCORING_VERSION` bumps on formula/methodology change only** — data
  refreshes rescore under the same version. So: same version + two as-of
  dates = data movement; version change = methodology movement. Never
  both in one attributable step without saying so.
- **Band cuts are fixed absolute values** (35/60/90 since 2026-08-11,
  Option B), revisited on methodology change only — a material's band
  never moves because another material moved, and never because of a
  routine data refresh (churn-risk set within ±2 pts of a cut is
  disclosed in the band proposal §4).
- **Corpus repair is re-ingest, not backfill.** Defects found in ingested
  data are fixed in the ingest command and the source is re-run
  (idempotent cleanups live inside the commands). No one-off mutation
  scripts against share tables.

## 8. What the public page (A6) states from this

As-of + per-stage source/vintage labeling on every score; the annual
update cadence and why it's appropriate for a structural metric; the
data-year convention; the denominator/RoW treatment; the freshness gate
and stale-stage banners; the single accepted staleness (§5) in plain
language; and that live events are shown beside — not inside — the score.

## 9. Sign-off

- [x] Nicole — date:
- [x] Partner — date:
- Notes / amendments:
