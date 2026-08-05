# Concentration-First Scoring Launch — Plan

*Drafted 2026-08-04 from Nicole's direction. PLANNING DOCUMENT ONLY — no
code, workbook, or data changes have been made. Everything cited was
verified against the repo on 2026-08-04. Supersedes the Phase 6 flip as
written in `event_triage_pipeline_plan.md` §5 once approved (that doc's
status ledger should be updated on approval, not before).*

## 0. The decision and why

Launch scoring on the **material concentration pillar alone**. Demote the
four event-driven pillars to computed-but-not-published while their
categorization and weighting are validated against live triaged data.
Verified events still surface — as the content feed and as per-material
signal context — they just don't move a published number yet.

Grounds, all verified in this repo:

1. **The event pipeline has demonstrated systematic quality problems** that
   the event pillars inherit wholesale: 55% of the pre-audit GTA corpus was
   subsidies narrated as financial-pressure risk; every EU-wide measure was
   geo-tagged Austria (`_resolve_iso3_jurisdiction` took `list[0]`); a
   suspended, never-in-force export ban carried 0.99 severity; unrelated
   events surface as duplicate hints; non-ISO region codes (XJ) pass
   through unvalidated. Two of these are fixed for new rows; the corpus
   repair waits on reset #2, and more issues surface with every triage
   session — that is what triage is for.
2. **The confirmed-event corpus is too thin to score from.** The original
   Phase 6 plan flips scoring to confirmed-only input, but at current
   triage depth that means event pillars computed from a handful of
   events, with scores that would swing on *triage order*. Not conservative
   — unstable.
3. **The concentration pillar is the audited one.** V1 4.0/4.1
   (`stage_concentration.py`, spec `scoring_v1_spec.md`): pure function of
   per-stage production-share tables (USGS MCS at ore, benchmark workbooks
   downstream), stage-max with an HHI cliff, freshness-gated, parent/child
   deduped, unit-tested without a database. Since 4.1 it carries the
   EU-CRMA-aligned **WGI governance amplifier** — and WGI was removed from
   the geopolitical pillar at the same time to prevent double-counting. So
   "production shares × governance indicators, the way the IEA does it"
   is not distributed across pillars; it IS this pillar. Demoting the
   others loses none of the methodology.
4. **It is already the heaviest pillar** — MARKET_PILLAR_WEIGHTS material
   ≈ 0.333 after the V1 renormalisation.
5. **The demotion mechanism has precedent in this codebase.** Financial
   pressure was demoted exactly this way in V1 4.0: weight 0.0, still
   computed, persisted, and shown as material-level context, with every
   downstream consumer degrading cleanly (see the MARKET_PILLAR_WEIGHTS
   comment). This plan generalises a pattern that already shipped, not a
   new invention.
6. **Honesty upgrade, not downgrade.** What is published today is frozen
   scores computed from the pre-audit corpus. Replacing a broad number
   built on known-bad inputs with a narrow number built on auditable
   inputs is narrowing claims to the evidence — for a product selling data
   credibility, that is forward.

The named risk this plan must manage: **a structural score cannot see
disruption** (the DRC quota halved cobalt exports; a concentration score
does not move), and **"temporary" demotions become permanent unless exit
criteria are written down**. Workstreams C and D exist for exactly these.

## 1. Target state (what a user sees at cutover)

- **Published score** = material concentration, labeled as *structural
  supply risk*, with its as-of date prominent. Global per-material and
  market (material × geography) surfaces both work — the pillar is
  computed per-geo, so market scores remain meaningful.
- **Four pillars displayed as "signals — in validation"**: computed and
  persisted (shadow-scored) but weight 0 in the overall, visually
  distinguished, with copy explaining they publish when validated.
- **Live-signal context per material**: a non-scored indicator (confirmed
  events in the 90-day window, worst severity) sitting beside the
  structural score. Data support + event support, no weighting to defend.
- **Content feed** (old Phase 7): scoring + display_only events, newest
  first — elevated in priority, because while the score is narrow the
  events ARE the live half of the product.
- **Company scores: paused/labeled** — they lean on obligations + event
  evidence and would hollow out silently otherwise.

## 2. Workstream A — prove concentration can carry it (BLOCKING, first)

The pillar must be audited before it stands alone. Known debts, in order:

- **A1. Benchmark share audit.** `benchmark_shares_v3.xlsx` carries rows
  marked audited=N (incl. the 18 REE-children/Ge refined-stage rows from
  2026-07-26) plus IEA draft rows. Nicole audits → load → rescore. Without
  this the downstream-stage shares feeding stage-max are unverified.
- **A2. Freshness exposure report.** Stage-max + the freshness gate means a
  material whose STRONGEST stage share is stale silently scores off a
  weaker stage — understating exactly the chokepoints that matter (the
  spec's own example: cobalt battery-grade CN 85% @2022 is display-only at
  a 2026 as-of). Needed: a per-launch-list-material report of which stages
  are fresh vs stale at the scoring as-of, and which materials' scores are
  currently degraded by staleness. This is new (small) tooling and the
  single biggest unknown in whether the pillar is launch-ready.
- **A3. USGS renormalisation bug** — deferred in the share-coverage notes;
  must be resolved or explicitly accepted before solo launch.
- **A4. WGI vintage + cadence.** Verify which WGI year is loaded and
  document the refresh cycle. The amplifier is only as current as its
  percentiles.
- **A5. Cadence + versioning statement.** Concentration updates on data
  releases (MCS annual, benchmark periodic), not on news. That is
  acceptable BY DESIGN (Nicole: "even if it's delayed by time") but must
  be stated: every published score carries as_of and source vintages.
- **A6. Public methodology page.** The defensibility asset: inputs (USGS
  MCS, World Bank WGI, benchmark capacity), formula (stage-max HHI cliff ×
  sqrt share, governance amplifier), precedent (HHI standard, CRMA-aligned
  amplifier, IEA-style inputs). Publishing the method is what makes
  "narrow but defensible" a selling point rather than an apology.
- **A7. Overall-score semantics + bands recalibration.** Decide what
  `overall_risk_score` means (recommended: concentration relabeled, other
  weights 0 via the financial-pillar pattern). Then recalibrate bands:
  30/50/65 was calibrated against the 4.3 five-pillar distribution
  (`bands.py`: 7 CRIT / 13 HIGH / 11 MOD / 9 LOW); a concentration-only
  distribution differs, so every band assignment shifts. Re-cut against
  the new distribution, re-verify the CRIT membership list, and sweep
  editorial/regulation-page copy for band references. Bands are fixed
  absolute cuts by design — keep that property.

## 3. Workstream B — demotion mechanics (small, after A is credible)

- **B1. Weights, not machinery.** Set geopolitical/regulatory/operational
  weights to 0 in MARKET_PILLAR_WEIGHTS (financial already is), material
  renormalises to 1.0. Pillars keep computing and persisting — this is the
  shadow-scoring requirement: the validation record ("field testing")
  needs shadow scores to diff against reality and against triage outcomes.
  No schema change.
- **B2. Cutover replaces the frozen scores** in one attributable step:
  frozen 5-pillar (pre-audit corpus) → live concentration-only (audited
  inputs). Diff and review before publishing, per the existing rescore
  discipline. Scheduled scoring stays off until cutover day; shadow runs
  are manual.
- **B3. Company scores** paused or labeled "structural context only" —
  explicit decision needed (open question Q5).
- **B4. UI copy sweep.** The triage queue currently promises "nothing
  scores until an analyst confirms" — implying confirming DOES score.
  Under this plan confirmed events feed the FEED and the VALIDATION
  record, not the score. Reframe triage as validation + content work
  (which it genuinely is: triage labels are the evidence that re-promotes
  pillars), or analysts will conclude the queue is pointless. Dashboard
  and score-page copy likewise.

## 4. Workstream C — bridging (score ↔ feed coexistence)

- **C1. Labeling**: published score = "structural supply risk"; events =
  "live signals". The score page states plainly: current events are shown
  alongside and are not yet folded into the score, and why.
- **C2. Live-signal indicator** per material: confirmed, direct-linked
  events in the 90-day window + worst severity — already computable from
  the triage coverage endpoint's counting rules. NOT a score; a count.
  This is the answer to "the DRC quota halved cobalt exports and the
  number didn't move" — the number isn't supposed to; the signal sits
  next to it.
- **C3. Content feed endpoint** (old Phase 7, promoted): scoring +
  display_only, newest first, full summaries + permalinks. While scoring
  is narrow, this is the live half of the product and the main visible
  payoff of triage work.
- **C4. Event-support on material pages**: verified events listed under
  each material (the feed filtered by material) so "data support + event
  support" is visible in one place.

## 5. Workstream D — re-promotion framework (the exit criteria)

**D0. General gate — a pillar re-publishes when ALL of:**

1. Its input path is audited (per-pillar list below).
2. Coverage: ≥ N confirmed events feeding it across the launch list
   (N per pillar, set by Nicole+partner — see Q1).
3. Suggestion quality: machine confirm-rate ≥ X% per source/match_reason
   from triage statistics (X to set; requires the stats surface, D-dep
   below).
4. Shadow-score review: the pillar's shadow scores over the trailing
   window are stable and explainable; top-impact events spot-audited
   clean.
5. Partner sign-off, recorded in this doc's ledger.

**Dependency for every gate: the triage-statistics surface does not exist
yet** (confirm/correct/dismiss rates per source and match_reason — the §7
"suggestion-engine quality measurement"). It becomes required tooling, not
a nice-to-have, and should be built early so evidence accumulates from the
start.

**D1. Regulatory — first back, in two halves.** The obligation uplift
(0–40) is curated workbook stock with NO event parsing anywhere in it —
already near launch-grade. The event rollup (0–60) waits with the others.
Path:
  - Land the pillar-reassignment (migration 067 + workbook edits per the
    evidence memo) so the uplift stops carrying flow-measure double-counts
    — precondition, already designed.
  - Audit `geography_compliance_weights` coverage: rows without curated
    JSONB fall back to a default — count how many and curate the gaps
    (unknown today; part of the audit).
  - Decide uplift-only presentation (Q3): publish as "regulatory
    obligations" sub-score on its own 0–40-derived scale, or rescale.
  - Event half re-promotes later under the D0 gate using Federal
    Register / EUR-Lex confirm rates.
**D2. Geopolitical — second.** Largest event dependency. Preconditions:
reset #2 applied (EU-collapse + severity fixes in corpus), GTA
event-date-on-revision semantics verified (unresolved suspicion: amended
old measures may read as fresh signal), standing floors landed (evidence
memo values), GTA confirm-rates ≥ threshold, floor review dates current
(Nov 2026 suspension cliffs, Jan 2027 Zimbabwe). Destination-blindness
documented as a known limitation at re-promotion.
**D3. Operational — third.** The operational-news candidate pipeline has
not run in production; facility coverage has known launch blockers
(Phosphate: zero facilities). Needs: pipeline running, promote flow
exercised in triage, facility-coverage gaps closed for the launch list.
**D4. Financial pressure — last.** Already weight-0 at market level;
depends on the positive-policy design question (do subsidies reduce risk?)
and the subsidy re-categorization holding up in triage stats.

## 6. Sequencing

**α — now → reset #2 (parallel with ongoing triage):**
  1. A1 benchmark audit (Nicole) → A3 renorm bug → A2 freshness report
     (new tooling) → A4 WGI check. These decide GO/NO-GO for solo launch.
  2. Build the triage-statistics surface (D-dep) so validation evidence
     accumulates from now.
  3. Fix batch keeps accumulating (467/067 pillar reassignment
     implementation once design approved; 7204 seed gap; duplicate-hints;
     GTA date semantics verification).
**β — cutover (the new Phase 6):**
  4. Reset #2 (delete + re-ingest with the fix batch; preserves nothing in
     the queue — timed while triage investment is still small).
  5. B1–B4 demotion + A7 bands + C1/C2 labeling and live-signal indicator.
  6. Cutover rescore: frozen → concentration-only. Diff, review, publish
     with A6 methodology page.
**γ — content:**
  7. C3 feed endpoint + C4 material-page events (can start during α/β —
     independent of scoring).
**δ — earn-back:**
  8. D1 regulatory uplift (first candidate; needs 067 landed + weights
     audit).
  9. D2 geopolitical, D3 operational, D4 financial as gates clear.

Hard orderings: A1–A4 before cutover (a solo pillar must be audited
first). 067 before D1. Reset #2 before D2 evidence means anything. The
stats surface before ANY re-promotion gate can be evaluated. C1/C2 ship
WITH cutover, not after — the score must never appear without its bridge.

## 7. Open questions (decide before the affected step)

- **Q1.** Re-promotion thresholds: N confirmed events and X% confirm-rate
  per pillar — Nicole + partner set numbers; the framework is ready
  without them but the gate can't fire.
- **Q2.** Bands: re-cut values for the concentration-only distribution
  (needs A2/A1 done first so the distribution is real).
- **Q3.** Regulatory uplift-only presentation and scale.
- **Q4.** Overall-score label on the public site ("Structural Supply
  Risk" vs keeping "Risk Score" with a methodology qualifier).
- **Q5.** Company scores: pause entirely vs label as structural context.
- **Q6.** Shadow-scoring cadence (manual monthly? per-reset?) — scheduled
  scoring stays off either way until cutover.
- **Q7.** Whether the market-scores page ships at cutover (nav link still
  commented out) — concentration-only market scores are meaningful, but
  it's additional surface to defend on day one.
- **Q8.** What the frozen scores' final disposition is — archived for the
  cutover diff, then removed from all surfaces.

## 8. Known gaps this plan accepts (stated, not hidden)

- The published score will not react to live disruptions; the live-signal
  indicator and feed carry that, and the methodology page says so.
- Concentration data refreshes on source cadence (annual MCS, periodic
  benchmark) — slow by construction; as-of labeling carries the honesty.
- Destination-specific exposure (e.g. China's US-military prohibition) is
  invisible to per-country scoring — documented limitation, roadmap item.
- Sub-USGS-threshold producers have no share rows and score 0 by design
  (4.1 removed the phantom-producer fallbacks) — "no data" renders as
  low-risk absence unless the UI distinguishes no-signal, which the
  sub_input_diagnostic machinery supports and the score pages should use.
- Event pillars go dark publicly for an unknown duration — the exit
  criteria (D0) are the guarantee this is temporary; without agreed
  thresholds (Q1) that guarantee is not yet real.
