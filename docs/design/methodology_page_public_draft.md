# A6 — Public Methodology Page (copy draft)

*Drafted 2026-08-16, revised same day (Nicole's review: scope the copy to
what the CONTENT SITE actually shows — risk bands and an as-of date; no
events surfaced publicly yet, no numeric scores, no per-stage displays.
Platform-only surfaces must not be described as if public). Workstream A
item A6, written in site voice. Square-bracketed items are open decisions.
Once approved it gets built as a hub page (proposed route:
`/intelligence/methodology`) and linked from the sidebar risk block.
Internal counterpart: `concentration_cadence_versioning.md` (A5).*

---

# How we score supply risk

## What the rating measures

The risk ratings on this site measure **[structural supply risk / Q4
label TBD]**: how concentrated the supply of a material is, at its most
concentrated processing stage, weighted by the governance quality of the
country that dominates it.

It answers one question: *if you needed this material, how exposed are you
to a single country's chokehold on it?* It is deliberately a structural
measure — it describes how the supply chain is built, not what happened
this week. Our reporting covers live developments (export bans, quotas,
sanctions, disruptions) as they happen; the rating moves only when the
structure of supply actually shifts. A measure that mixed slow structure
with fast news would answer neither question well.

## How it works

**1. Supply shares by processing stage.** For each material we maintain
country-level production shares at every stage of the chain we can source
from citable data — mining, intermediate processing, refining, and
battery-grade conversion. Shares are computed against world totals that
*include* unattributed rest-of-world supply, so a country's share is never
inflated by an incomplete denominator.

**2. Concentration per stage.** Each stage is scored from two things: how
concentrated that stage is globally (measured with the
Herfindahl–Hirschman Index, the standard concentration measure used in
antitrust review), and how large the leading country's share of it is. A
stage that is both highly concentrated and dominated by one country scores
near the top of the scale.

**3. The strongest chokepoint defines the material.** A material's rating
is set by its *most* concentrated stage — not an average across stages. If
a material is mined in a dozen countries but 90% of its battery-grade
conversion happens in one, the conversion stage sets the rating. Averaging
would dilute exactly the risk that matters.

**4. Governance adjustment.** The driving stage's result is then adjusted upward
when the dominant country scores poorly on the World Bank's Worldwide
Governance Indicators — the same logic the EU Critical Raw Materials Act
applies: a 75% share held in a fragile jurisdiction is riskier than the
same share held in a stable one. The adjustment is bounded and never
manufactures risk where concentration is low.

## Risk bands

The bands you see on this site — **Low**, **Moderate**, **High**,
**Critical** — come from fixed cut-offs on an underlying 0–100 scale
(35 / 60 / 90). Critical is reserved for materials where a single country
controls roughly 80% or more of the binding stage — where no meaningful
alternative supply exists today. The cut-offs are absolute, not graded on
a curve: a material's band never changes because a different material
moved, and bands are revisited only when the methodology itself changes.

Materials without sufficient stage data to rate are excluded rather than
shown — absence of data is never presented as low risk.

## Data sources

| Input | Source | Updated |
|---|---|---|
| Mine-stage production shares | USGS Mineral Commodity Summaries | Annually |
| Midstream & battery-grade shares | IEA Global Critical Minerals Outlook (CC BY 4.0); Cobalt Institute Cobalt Market Report (data by Benchmark Mineral Intelligence); specialist sources per material | Annually |
| Governance indicators | World Bank Worldwide Governance Indicators | Annually |

Every share we load is human-verified against its cited source before it
can affect a rating — machine-extracted numbers never contribute unreviewed, and
we never estimate a share a source doesn't state.

## Freshness, honestly

Every rating carries an **as-of date**. Behind it, every stage input is
tracked by source and data year, and share data qualifies only while its
data year is within 24 months of the as-of date — older data is excluded
from the rating rather than quietly kept. Where the freshness rule
excludes a stage we know to be concentrated, the published rating errs
conservative: it can understate risk, never overstate it, and we say so
rather than estimate a number no source states.

Because the inputs publish annually, ratings update on an annual cycle.
That is the appropriate cadence for a structural measure; our reporting
carries what changed this month.

## What the rating does not capture

We would rather state the limits than have you discover them:

- **It does not react to live disruptions.** An export ban does not move
  the rating the day it lands — it reaches the structural data when
  supply shares actually shift. Our analysis and reporting cover those
  developments as they happen.
- **It measures where supply is produced, not where it goes.** Exposure
  that depends on a specific destination (e.g. restrictions targeting one
  importing country) is outside what the rating measures.
- **It only sees stages with citable data.** A stage with no published,
  verifiable share data cannot raise a rating — coverage grows as sources
  allow, and we treat a thin ladder as understatement, not as safety.
- **Additional risk dimensions — trade policy, regulatory exposure,
  operational disruptions, financial pressure — are measured but not yet
  published.** They enter the headline rating only after validation
  against a human-reviewed baseline meets pre-agreed quality gates. We
  publish the audited number, not the aspirational one.

## Precedent

The components are deliberately standard: HHI is the concentration measure
used by the U.S. DOJ/FTC in merger review; production-share ×
governance-quality weighting follows the approach of the EU Critical Raw
Materials Act and the IEA's supply-concentration analyses. Our
contribution is the per-stage supply-share dataset, the stage-max
framing, and the freshness discipline — not a novel formula.

## Versioning

Ratings are recomputed in full from stored evidence — never adjusted
incrementally — so any published rating can be reproduced from its as-of
date. Methodology changes are versioned and noted on this page;
[changelog placement TBD]. Data refreshes are visible as new as-of dates
with unchanged methodology.

---

*Draft ledger (internal):*
- [ ] Nicole — approve copy / edits, date:
- [ ] Partner — approve copy, date:
- Open: Q4 headline label wording; changelog placement; route + nav link.
- 2026-08-16 revision: platform-only display claims removed (events beside
  scores, per-stage sub-score display, stale banners, numeric scores) —
  copy now matches what the content site shows: bands + as-of date. The
  sulfate case generalized to an unnamed conservative-understatement
  statement. When events/numbers DO ship publicly (C2/C3), restore the
  stronger "shown alongside" language — the first draft's phrasing is in
  git history.
