# Regulation Pillar Reassignment — Stock vs Flow

*Drafted 2026-08-04, per Nicole's question during triage: "what should be
standing regulations and what should be events? What functionally is the
difference between the regulatory pillar and the geopolitical one?"
Design only — no code or workbook changes until reviewed. Everything cited
below was verified against the repo and the live workbook on 2026-08-04.*

## 1. The problem, measured

Three defects share one root cause.

**Double-counting.** The regulation workbook's supply-side rows carry
obligation points into the regulatory pillar's non-decaying 0–40 uplift
(`regulatory_risk.py`, soft-capped curve): ZW_LITHIUM_EXPORT_BAN 15,
CN_REE_EXPORT_2025 20, ID_NICKEL_ORE_BAN 12, DRC_COBALT_QUOTA_2025 10,
NA_UNPROCESSED_MINERALS_BAN 8, CN_MINOR_METALS_2025 12, CN_DUAL_USE_EXPORT
15, CN_REE_MGMT_2024 10, CL_LITHIUM_STRATEGY 5, MX_LITHIUM_NATIONALIZATION 3
(read from regulations_workbook_v1.xlsx). The same underlying measures also
generate GTA events classified EXPORT_RESTRICTION / TARIFF, which feed the
geopolitical pillar's `export_restriction_exposure` / `tariff_exposure`
(`market_aggregator._derive_market_geopolitical_inputs`). One fact —
"Indonesia will not export nickel ore" — inflates two pillars under two
names.

**Mislabelled risk.** An OEM has no *obligation* under Zimbabwe's export
ban; it has a supply cutoff. Scoring that as compliance burden misstates
what the number means, which matters because pillar decomposition is the
product's explanatory story.

**The decay gap.** Geopolitical evidence lives inside a finite window
(730-day ceiling, `evidence_query.py: RISK_EVENT_EVIDENCE_WINDOW_DAYS`;
GEOPOLITICAL_TRADE is the longest finite `EVIDENCE_WINDOW`). A standing ban
whose announcement events have aged out **disappears from the geopolitical
pillar entirely** while the compliance pillar keeps paying rent on it. The
Indonesia ore ban predates the window; today its persistence is recorded in
the wrong pillar, and if the workbook row were simply deleted it would be
recorded nowhere.

## 2. The principle

The pillars are distinguished by **function, not legal form**:

- **Regulatory compliance** answers: *will standing law impose cost,
  liability, or disqualification on companies in this chain?* Due
  diligence, traceability, disclosure, import eligibility.
- **Geopolitical trade** answers: *will the material stop moving, or get
  more expensive to move?* Bans, tariffs, quotas, sanctions,
  resource nationalism.

Orthogonal to that, the registry/event split is **stock vs flow**:

- An **event** is anything with a date that *changes* state — announcement,
  amendment, suspension, enforcement action. It decays. It can feed either
  pillar.
- A **workbook regulation** is a regime *currently in force* whose ongoing
  existence burdens the chain. It persists until the regime changes, and it
  feeds **exactly one pillar's standing layer per weight** — chosen by
  function.

Crossover between registry and events is correct and unavoidable (every
regime's birth and death is an event). What must not cross over is the
standing weight: one regime, one pillar per standing contribution.

This also settles the triage rule from the EU–US countermeasures
walkthrough: a suspended measure has no standing entry (its events are
display_only); a measure entering force gets a workbook row tagged by
function, and the entry-into-force event still fires and decays normally.

## 3. Design

### 3.1 Schema (migration 067)

Two orthogonal standing weights on `regulations`, plus a display
classification:

```
pillar                        String(64)  — 'regulatory_compliance' |
                                            'geopolitical_trade' | 'dual'
                                            (display/primary classification)
standing_export_restriction   Float NULL  — [0,1] persistent floor under the
                                            geo pillar's export sub-input
standing_tariff_exposure      Float NULL  — [0,1] persistent floor under the
                                            geo pillar's tariff sub-input
floor_review_date             Date NULL   — when this floor is known to
                                            change character (suspension
                                            expiry, phased ban taking
                                            effect); surfaced as a workbook
                                            chore, not enforced in code
```

`floor_review_date` was added by the evidence pass
(`standing_measure_floor_evidence.md`): three Chinese suspensions expire
2026-11-10/27 and Zimbabwe's concentrate ban lands 2027-01-01 — floors are
perishable, and a static number with no review hook goes stale silently.

Two numeric fields rather than a single enum-driven switch, because the
dual-natured regimes are real: China's export-control regimes are licensing
burdens on companies (compliance stock, reduced `obligation_points`) *and*
flow interference (geo floor). The enum records the primary character for
UI grouping; the numbers decide the arithmetic, so nothing is
double-counted by construction — each weight feeds one pillar.

`status` vocabulary gains `suspended` (today: proposed | enacted |
effective | superseded). Only `enacted`/`effective` rows contribute either
standing weight.

### 3.2 Workbook + loader

`Regulations` sheet gains `pillar`, `standing_export_restriction`,
`standing_tariff_exposure` (synced via `_SYNCED_FIELDS` in
`regulation_workbook.py`). Loader validation, same reject-report pattern as
existing sheets:

- floors ∈ [0,1]; reject otherwise.
- `pillar = geopolitical_trade` ⇒ `obligation_points` must be empty/0
  (the whole point is ending the double-count); `dual` may carry both,
  each reduced, with a required `source_note` explaining the split.
- a non-null floor requires `geography` (ISO2 of the implementing state)
  and at least one material scope row (or `applies_all_materials`) — a
  floor with no scope would apply nowhere.
- `status = suspended` ⇒ warn if any standing weight is non-zero.

### 3.3 Scoring: the geo floor

In `_derive_market_geopolitical_inputs` (market_aggregator.py), the
combination today is `max(event_export, hs_export)` / `max(event_tariff,
hs_tariff)`. Add a third term:

```
standing = regs where status in (enacted, effective)
           and geography == geography_code
           and (applies_all_materials or material_id in scopes)
floor_export = max(standing_export_restriction × enforcement_weight(material))
floor_tariff = max(standing_tariff_exposure × enforcement_weight(material))
export_exposure = max(event_export, hs_export, floor_export)
tariff_exposure = max(event_tariff, hs_tariff, floor_tariff)
```

`max()`, not sum — the floor is "this regime guarantees at least this much
exposure", and when fresh events exceed it (a new escalation), the events
win. Reuses `material_enforcement_weights` (065) so an all-goods control
that enforces unevenly stays uneven. `sub_input_diagnostic` gains a
`standing_floor` source entry naming the regulation_key, so the partner UI
can say "floored by ID_NICKEL_ORE_BAN" instead of showing an unexplained
number — same transparency contract as 11.4-Geo.

### 3.4 Scoring: the compliance uplift

No code change. Zeroing `obligation_points` on flow rows in the workbook
removes them from the uplift automatically (`coalesce(points, 0)` on both
the market path and the company path). The uplift becomes what its name
claims: compliance burden only.

## 4. Row-by-row classification — evidence-calibrated 2026-08-04

*Original version of this table was an ordinal placeholder seeded from the
obligation points' ranking. It has been replaced following the evidence
pass — see `standing_measure_floor_evidence.md` for per-measure sources,
the calibration model (channel-blocked × enforcement), and what the
evidence overturned (headline: the two outright ore bans are the NARROWEST
measures, the DRC "quota" is the strongest, and three rows don't belong in
the floor at all). Floors remain curation subject to your + partner audit,
but they are now grounded, and each carries its perishability date.*
Compliance rows are unchanged (listed for completeness).

| regulation_key | pillar | obligation_pts (now → proposed) | standing floor (proposed) |
| --- | --- | --- | --- |
| UFLPA | regulatory_compliance | 25 → 25 | — |
| EU_BATTERY_REG_2023 | regulatory_compliance | 20 → 20 | — |
| CRMA_2024 | regulatory_compliance | 15 → 15 | — |
| IRA_DOMESTIC | regulatory_compliance | 15 → 15 | — |
| EU_CSDDD | regulatory_compliance | 10 → 10 | — |
| EU_REACH_COBALT | regulatory_compliance | 8 → 8 | — |
| EU_CBAM | regulatory_compliance | 5 → 5 | — |
| EU_CONFLICT_MINERALS | regulatory_compliance | 3 → 3 | — |
| SEC_CLIMATE_2024 | regulatory_compliance | 0 → 0 (court-stayed) | — |
| DODD_FRANK_1502 | regulatory_compliance | 8 → 8 | — |
| EU_FLR_2024 | regulatory_compliance | 5 → 5 | — |
| CA_S211 | regulatory_compliance | 3 → 3 | — |
| US_DFARS_SPECIALTY_METALS | regulatory_compliance | 5 → 5 | — |
| JP_ESPA_CRITICAL_MATERIALS | regulatory_compliance | 3 → 3 | — |
| US_WRO_HOSHINE | regulatory_compliance | 8 → 8 | — (import-eligibility burden; UFLPA-family) |
| ZW_LITHIUM_EXPORT_BAN | geopolitical_trade | 15 → 0 | export **0.45** · review 2027-01-01 (steps to ~0.80 if the concentrate ban lands with one sulphate plant built) |
| ID_NICKEL_ORE_BAN | geopolitical_trade | 12 → 0 | export **0.35** (ore channel totally blocked; nickel units exit freely as NPI/matte/MHP) |
| NA_UNPROCESSED_MINERALS_BAN | geopolitical_trade | 8 → 0 | export **0.20** (ore-only, concentrate exempt, enforcement porous) |
| DRC_COBALT_QUOTA_2025 | geopolitical_trade | 10 → 0 | export **0.55** · review 2027-12-31 (caps ALL forms at ~50% of prior exports; over-binding in practice) |
| CN_MINOR_METALS_2025 | geopolitical_trade | 12 → 0 | export **0.50** · review 2026-11-27 (US-ban snap-back → 0.85–0.95 for US flows) |
| CN_REE_MGMT_2024 | geopolitical_trade | 10 → 0 | **no floor** — domestic production-control statute, no export mechanism; registry/display row + events only (natural home: a future production-control factor) |
| CN_REE_EXPORT_2025 | dual | 20 → 8 (licence-process burden) | export **0.30** · review 2026-11-27 (licensing that mostly approves; record 2025 exports; permanent US-military zero) |
| CN_DUAL_USE_EXPORT | dual | 15 → 6 | export **0.30** · review 2026-11-10 (friction not blockage; anode-controls revival → 0.55–0.65) |
| CL_LITHIUM_STRATEGY | geopolitical_trade | 5 → 0 | export **0.00–0.05** — ownership restructuring, exports at record; registry/display row, effectively events-only |
| MX_LITHIUM_NATIONALIZATION | geopolitical_trade | 3 → 0 | export **0.00** — no production exists to restrict; drop standing weights, events only |

No current row warrants a `standing_tariff_exposure` — the column exists
for the case flagged in triage: the US steel/aluminium tariff regime, if
you decide it belongs in the registry, would be the first (tariff floor on
Aluminum × US, in force, expanded through 2025). That decision is
deliberately out of scope here.

## 5. Blast radius and rollout

Effects at next rescore: regulatory sub-scores **drop** wherever flow rows
were contributing uplift (Li×ZW, Ni×ID, REE×CN, Co×CD, minor-metals×CN
pairs most visibly — the CN REE pair loses 20 weighted points of raw
uplift, partially offset for `dual` rows). Geopolitical sub-scores **rise
or hold** wherever floors exceed currently-decayed event signal — the exact
pairs that were quietly losing their standing bans to the 730-day horizon.
Overall scores move in both directions; that is the point, not a
regression.

Rollout, honouring the no-backfill directive and the frozen-scores rule:

1. Land migration 067 + loader + aggregator floor + workbook edits as part
   of the accumulating fix batch. Nothing recomputes on landing (scheduled
   scoring is off; scores are frozen grandfather values until Phase 6).
2. At the Phase 6 flip, rescore in **two stages for attribution**: stage A
   = confirmed-only flip alone; stage B = A + pillar reassignment. Diff
   A→B is exactly this change's fingerprint; diff frozen→A is curation's.
   One combined rescore would leave score movements unexplainable, and
   "explain any sharp mover before publishing" is already the Phase 6 rule.
3. The regulation pages' editorial (workbook `Editorial` sheet) should get
   a sentence on reclassified rows — the public page currently narrates
   these as compliance regimes.

## 6. Open questions — updated after the evidence pass

1. ~~Floor values~~ — now evidence-calibrated (see companion memo); audit
   the *calibration model* (channel-blocked × enforcement, concentration
   deliberately NOT folded in because the pillar multiplies it separately)
   rather than each number in isolation.
2. `dual` splits for the two CN export-control regimes — **evidence
   supports keeping them**: 45–120-day licence timelines, per-shipment
   end-use certifications demanded of buyers, and active 2026 criminal
   enforcement are a real compliance burden independent of whether
   shipments flow. Magnitudes (8/6 points) still yours to set.
3. US_WRO_HOSHINE: kept compliance (import-eligibility/provenance burden);
   still the one genuinely ambiguous row.
4. Whether the US steel/aluminium tariff regime enters the registry as the
   first `standing_tariff_exposure` row (raised 2026-08-04, parked).
5. ~~Whether CL/MX belong~~ — **answered by evidence: they don't carry
   standing weights.** Chile's exports grew every year under the strategy
   (and China's SAMR approval *requires* continued supply); Mexico has no
   production to restrict. Both stay as registry/display rows with events;
   CN_REE_MGMT_2024 joins them (domestic production-control, not export
   restriction).
6. NEW — destination blindness: the floor is per implementing geography
   and cannot represent destination-specific measures (China's permanent
   US-military-end-use prohibition; the Dec 2024–Nov 2025 US-only ban).
   If partner customers are US-concentrated, the CN floors understate
   their exposure. Out of scope for 067; flagged for the roadmap.
7. NEW — whether a production-control factor should exist (concentration
   side) to carry CN_REE_MGMT_2024-class regimes: state quota systems
   discipline supply without restricting exports, which today's model can
   only see through events.
