# Signed impact vs. a separate mitigation term — analysis

**Status:** analysis for decision, 2026-07-28. No code changes proposed here beyond the
routing correction in §2, which is a bug fix rather than a methodology choice.
**Supersedes the premise of** `docs/design/positive_policy_scoring.md` (2026-07-13), which
states that POSITIVE_POLICY events "route nowhere in the market_aggregator" and that
POSITIVE_DEVELOPMENT is "scoring-inert." Measured against the live database today, that is
true of one pillar out of five.

---

## 1. What is actually in the data

Canonical (non-duplicate) events carrying a `POSITIVE_*` subtype, live counts as of
2026-07-28:

| primary_category | count | severity range | mean |
|---|---|---|---|
| geopolitical_trade | 101 | 0.15 – 0.20 | 0.191 |
| operational | 55 | 0.10 – 0.40 | 0.206 |
| financial_pressure | 13 | 0.10 – 0.30 | 0.171 |
| regulatory_compliance | 12 | 0.15 – 0.30 | 0.213 |
| material_concentration | 1 | 0.20 | 0.200 |
| NULL (display-only) | 6 | 0.15 – 0.20 | 0.175 |

Every one of these severities was assigned as a *magnitude on a risk scale* — the IEA
ingester derives it from policy status bands, the manual walkthrough from human judgment
about how significant the news was. None of them was ever assigned as "how much risk does
this remove."

---

## 2. The routing leak: positives are not inert

Events reach a pillar through one gate only — `RiskEvent.primary_category == category.value`
in `evidence_query.py`. Whether they then move the score depends on what each pillar does
with the list it receives, and the five pillars disagree.

**Geopolitical (inert, by construction).** `_classify_geo_events` is an allowlist:
`EXPORT_SUBSIDY` → subsidy bucket, `EXPORT_RESTRICTION_SUBTYPES` → export bucket,
`TARIFF`/`TRADE_POLICY` → tariff bucket, plus title-text fallbacks. `POSITIVE_POLICY` matches
none of them and falls out of the loop entirely. I checked the text-fallback leak explicitly:
**zero** of the 101 events have a title containing the export/ban/control, tariff, or
section-301 patterns. This pillar behaves as the design doc describes.

**Operational (leaking, and this is the big one).** `_derive_market_operational_inputs`
builds `weighted_event_impacts` as an unfiltered comprehension over every event handed to it.
No subtype gate exists. All 55 positives contribute full `severity × confidence × recency ×
relevance` impact, and since V1 forces `struct_dep = None`, the pillar is 100% event-driven:
`score = mean(top 3 impacts) × 100`.

Measured over the 50 (material × country) cells that currently have any operational event:

- 90 top-3 slots are occupied; **29 of them (32%) are held by a POSITIVE_\* event.**
- 36 of the 50 cells hold ≤3 operational events, so in those cells *every* event is in the
  top 3 and there is no dilution at all.
- 27 cells hold exactly one operational event, and in **11 of them that single event is
  positive** — the entire operational pillar score for those cells is derived from good news.

Recomputing each cell with positives excluded (approximating recency at 1.0):

| cells | operational points today | if positives dropped | Δ operational | Δ market score (×0.133) |
|---|---|---|---|---|
| 1 | 37.1 | 0.0 | 37.1 | 4.94 |
| 4 | 24.7 | 0.0 | 24.7 | 3.29 |
| 1 | 20.0 | 0.0 | 20.0 | 2.67 |
| 5 | 18.5 | 0.0 | 18.5 | 2.47 |
| 1 | 12.4 | 0.0 | 12.4 | 1.65 |
| 4 | 37.6 – 45.3 | 32.3 – 41.2 | 0.6 – 6.2 | 0.08 – 0.82 |

Sixteen of fifty cells are inflated; twelve of them score their operational pillar entirely
off events that describe things going *right*. The 4.4 change to a top-3 mean (2026-07-27)
made this strictly worse in thin cells: under the previous all-event mean a positive was
diluted by the full tail, and now in any cell with ≤3 events it carries a full one-third — or
the whole — of the pillar.

**Financial (leaking, with a second-order amplifier).** The event loop in
`_derive_market_financial_inputs` is an if/elif chain ending in a catch-all `else` labelled
"generic financial pressure event." `POSITIVE_DEVELOPMENT` and `POSITIVE_POLICY` land there
and add `severity × 5.0` points to `base_filing_signal` — small in itself, roughly 0.85
points at the observed mean severity. The larger effect is that `filing_count` is
incremented by `len(fin_events)` regardless of subtype, and `score_financial_pressure`
applies `raw_score × (evidence_count / 2.0)` when `evidence_count < 2`. A single positive
event arriving in a cell that previously had one evidence point therefore **doubles** that
cell's financial pillar score. Cobalt already has `pink_sheet_price_points = 0`, so
sparse-evidence cells are not hypothetical here.

**Regulatory (inert by accident, not by design).** `_derive_market_regulatory_inputs` appends
`_event_impact` for every event returned by `get_events_for_regulations` with no subtype
filter — structurally the same leak as operational. It does not fire today only because all
12 positive regulatory events have zero rows in `risk_event_regulations`. The moment the
event→regulation linking work lands (19 of 25 regulations currently have zero linked events,
and that is on the backlog), this pillar starts leaking too.

**Material concentration (leaking, negligible).** `trade_volatility =
_avg_impact_normalised(trade_events, …)`, no subtype gate. One positive event, one cell.

The conclusion I would draw: this is a routing defect, not a methodology gap, and it is
independent of the signed-vs-mitigation question. Whichever of the two you pick, the first
change is the same — exclude `POSITIVE_*` from the event lists the risk pillars consume, in
all five pillars rather than only the one that happens to have an allowlist. That restores
the property the design doc already assumes and gives you a clean baseline to measure any
positive-signal design against. Doing it costs one helper and five call sites; deferring it
means every number in the comparison below is measured against a baseline that is itself
wrong.

---

## 3. Option A — signed impact

Give events a direction and let a positive event contribute a negative impact into the same
arithmetic that negative events already feed.

### What works

It is a small amount of code. There is one impact function (`compute_event_impact`) and one
call site per pillar; sign could be introduced at `_event_impact` by reading
`event_subtype`/`metadata.policy_direction` and negating the result. No migration, no new
storage, no new display surface.

It produces a single number. Users get one risk score whose movements they can attribute to
one event stream, rather than a risk number and a momentum number whose relationship they
have to construct themselves.

It composes with what already exists. Recency decay, confidence, relevance multipliers, and
the export-restriction half-weight fold all keep working on the magnitude; only the sign is
new. There is no parallel aggregation path to keep in sync across L0 → L1 → L2 forever.

Removal of a restriction is genuinely a same-axis event. When GTA Green lands (Layer 1b in
the existing doc), "export ban X lifted" and "export ban X imposed" measure the same physical
object on the same axis, and netting them is the semantically correct operation. Signed
impact handles that case natively; a separate mitigation term handles it awkwardly, because
you would be showing rising momentum next to a risk score that should simply have fallen.

### What breaks

*The aggregation functions are not sign-safe.* `_avg_impact_normalised` computes
`min(1.0, sum/len/MAX)` with no lower clamp, so a cell whose events net negative yields a
negative sub-input, which is then handed to scorers that assume `[0, 1]` and, in several
cases, validate it. `_max_impact_normalised` takes `max()` over the list — with signed values
that returns the *least negative* element when all events are positive, which is meaningless;
the correct operation would be max-by-magnitude-preserving-sign, which is a different
function. Each of these needs auditing individually.

*The top-3 selection becomes ill-defined, and the natural reading is backwards.*
`_score_operational_market` does `sorted(op_impacts, reverse=True)[:3]`. With signed values,
positives sort to the bottom and are selected only when a cell has fewer than three events —
so netting activates exactly in the thin-data cells where it is least defensible, and is
invisible in the well-populated cells where you would actually trust it. Sorting by magnitude
instead inverts the problem: a strong positive can evict a real disruption from the top 3 and
the disruption vanishes from the score entirely. There is no obviously right third option.

*The floor destroys information the codebase has invested in preserving.* Pillar scores are
`[0, 100]`. A cell with one moderate disruption and three supportive policies clamps to 0 —
identical to a cell with no data at all. The 11.4 audits went to considerable trouble to stop
"no data" and "no risk" from looking alike (`data_backed` flags, the `or 0.5` → `or 0.0`
severity fix, `struct_dep=None` rather than a 0.3 placeholder). Signed impact reintroduces
exactly that ambiguity at the pillar level, and `_compute_pillar_data_completeness` will
report the cell as well-covered while it reports zero risk.

*Cancellation semantics are wrong across different physical objects.* Netting says a
severity-0.20 Canadian tax credit removes as much risk as a severity-0.20 Indonesian smelter
outage adds. Those are not the same axis (this is Trap 1 in the existing doc, unchanged), and
the equivalence is asserted by the arithmetic rather than argued anywhere. Restricting
netting to same-object pairs would fix it, but that requires revocation linking — GTA
`state_act_id` chains — which does not exist and is explicitly out of scope in the current
design.

*The severities are not calibrated as netting magnitudes.* Backfilling 188 rows means
reinterpreting numbers assigned by two different processes (IEA status bands, human
walkthrough judgment) as an exchange rate against disruption severity. The IEA band spread is
0.15–0.20 across 101 events, which is close to no differentiation at all; whatever those
events subtract, they subtract nearly uniformly.

*Blast radius.* `compute_event_impact` is shared by all five pillars and the HS-node scorer.
Sign changes the semantics of every consumer at once, including ones not yet audited for it,
and there is no per-pillar opt-out short of adding one.

*The export-restriction half-weight fold needs a symmetric decision.*
`_export_restriction_operational_impacts` half-weights geopolitical events folded into
operational, on the reasoning that the event already counts at full weight elsewhere. If a
GTA Green liberalisation folds in the same way, it needs the same halving — and if it does
not, the two directions attenuate differently and the netting is asymmetric by construction.

---

## 4. Option B — separate mitigation term

Compute a parallel `mitigation_score` from `POSITIVE_*` events through the same machinery,
store it alongside risk, display it, and net it into risk only if and when a calibration
justifies it. This is Layer 2 of the existing design doc.

### What works

It cannot corrupt the risk score. The risk number keeps meaning "measured exposure," and
every property the audits established — no midpoint defaults, no silent imputation, data
completeness legible — survives untouched. If the mitigation model turns out to be wrong, it
is wrong in a column nobody's risk score depends on.

It separates the two calibration problems. "How much momentum does this policy represent" and
"how much risk does momentum remove" become independent questions, and the second one can
stay unanswered indefinitely without blocking the first. Under signed impact they are fused:
shipping sign means answering both simultaneously, with a default constant of 1.0.

It is honest about the axis mismatch instead of arithmetically denying it. Policy momentum is
a leading indicator over unbuilt capacity; risk is a measurement of current structure.
Displaying them side by side states that relationship rather than assuming a conversion rate.

It has a defined path to legitimate netting. Layer 3's forward-HHI is anchored in named
facilities with owners, countries, and capacities. If netting ever happens, that is the
channel with evidence behind it, and the mitigation term is the thing that feeds it.

### What breaks

*It is materially more work, and the work is permanent.* A migration adding columns to two
score tables, a scorer module, a second aggregation path through L0 → L1 → L2 that must be
maintained in parallel with the risk path for the life of the product, and display wiring on
every surface that shows a score. Against that, signed impact is a sign flip and an audit.

*Coverage is thin enough to be misleading.* 154 (material × country) cells have any positive
event; 846 distinct cells carry a score. A momentum column would be empty for **82%** of the
grid. An empty momentum cell will read as "no supportive policy here" when it means "we have
not ingested any," which is the same silent-default failure mode the 11.4 audits removed
elsewhere — reintroduced on a new surface. It needs its own `data_backed` treatment from day
one, which is more work again.

*Every parameter is uncalibrated and there is no ground truth to calibrate against.* The
current proposal is a 3× half-life "for partner to calibrate," a saturation function borrowed
from the risk side, and severity bands of 0.30/0.15. None of these can be validated against
an outcome, because the outcome — did the policy produce supply — takes years and the
platform has no historical panel to fit on. The number will be defensible in construction and
unfalsifiable in value.

*Users will net it in their heads, inconsistently.* Showing "Risk 0.72 · Momentum 0.41"
without a conversion rate does not prevent netting; it delegates it, uncontrolled, to each
reader. Some will treat momentum as a discount, some will ignore it, and the platform has no
way to know which. That is a genuine cost of the paired presentation, not a neutral one, and
it is worth weighing against the fact that a wrong-but-explicit constant is at least
auditable.

*It does not fix the leak.* If Layer 2 ships while §2 remains unaddressed, the same positive
event both raises the operational risk score and displays as favorable momentum — the worst
available combination, and one that is hard to explain to a partner who notices it.

*Same-object revocation is handled poorly.* When GTA Green lands, "export ban lifted" will
increment momentum while the original ban's risk contribution decays on its own schedule.
The score does not fall when the restriction is removed; it falls whenever the decay curve
says so. That is a visible wrongness in the case that is easiest for a user to check.

---

## 5. How I read the trade-off

The two options fail in opposite directions, and the asymmetry is worth stating plainly:
signed impact is cheap to build and expensive to get semantically right; the mitigation term
is expensive to build and each of its pieces is separately checkable. Neither is obviously
correct, and the choice depends on something the data cannot settle — whether the product's
claim is "one number that already accounts for everything" or "a risk measurement plus
context."

What the data does settle is the ordering. Both options are currently being evaluated against
a baseline where positives already reduce nothing and *raise* risk in three pillars, with a
measured effect of up to 37 operational points and ~5 market-score points in the worst cell.
That defect is independent of the choice, cheaper than either option, and unambiguously a
bug. Fixing it first also produces the number that should inform the choice: with positives
routed out, you can rescore and see how much of the current operational signal was real —
and if the answer is that a dozen cells drop to zero, the honest read is that the operational
pillar has a coverage problem that neither a sign convention nor a momentum column will
solve.

The narrower observation on same-object cases: if the near-term goal is that removing an
export ban lowers the score, neither option does that well. Signed impact nets it against
unrelated events; a mitigation term shows it in a separate column while risk decays on its
own clock. The operation that actually matters there is revocation linking, which is a third
piece of work and is currently scoped out of both.

## 6. Questions that would change the analysis

1. Is the product claim a single blended number, or risk plus context? This determines the
   choice more than any implementation detail does.
2. Does the partner accept that a cell can score 0 operational risk because supportive policy
   cancelled a real disruption, and that this is indistinguishable from having no data? If
   not, signed impact is out on the floor-collapse argument alone.
3. Is GTA Green (Layer 1b) being ingested before or after this decision? If before, the
   same-object revocation case becomes common enough that its poor handling under both
   options is the dominant consideration, and revocation linking moves up the queue.
4. Should the 55 operational `POSITIVE_DEVELOPMENT` rows — favorable *company* outcomes, per
   the 2026-07-13 vocabulary split — be in a geography × material pillar at all? They are
   currently the largest single source of the leak, and demoting them to
   `primary_category = NULL` (display-only) resolves most of §2 without touching either
   design.
