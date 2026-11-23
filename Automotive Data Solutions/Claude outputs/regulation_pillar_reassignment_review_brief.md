# Pillar Reassignment (067) — Partner Review Brief

*2026-09-20. One-page agenda for the review of
`docs/design/regulation_pillar_reassignment.md` (255 lines) +
`docs/design/standing_measure_floor_evidence.md` (144 lines). Design only —
no schema, code, or workbook changes have landed. This brief is a map, not
a substitute: the two docs carry the full argument and citations.*

## Why this exists (three defects, one root cause)

The regulation registry doesn't distinguish *compliance burden* from
*trade-flow interference*, so standing export bans sit in the compliance
pillar. Consequences:

1. **Double-counting** — supply-side workbook rows (ID nickel ore ban, CN
   REE controls, ZW lithium ban, DRC cobalt quota, …) feed the regulatory
   uplift while the same measures' GTA events feed the geopolitical pillar.
   One fact inflates two pillars.
2. **Mislabelled risk** — an OEM has no *obligation* under Zimbabwe's
   export ban; it has a supply cutoff. Pillar decomposition is the
   product's explanatory story, and this misstates it.
3. **The decay gap** — geopolitical event evidence expires after 730 days,
   so a standing ban older than the window vanishes from the geo pillar
   while compliance keeps paying rent on it. Delete the workbook row and
   the ban is recorded nowhere.

## The fix in one paragraph

Regulations gain a `pillar` classification and two standing-floor fields
(`standing_export_restriction`, `standing_tariff_exposure`, each [0,1])
plus a `floor_review_date`. Flow rows lose their obligation points and
instead contribute a persistent **floor** under the geo pillar's
export/tariff sub-inputs via `max(event_signal, hs_signal, floor)` — fresh
events win when they exceed it. Dual-natured regimes (the two CN
export-control regimes) keep reduced compliance points *and* a floor; each
number feeds exactly one pillar, so nothing double-counts by construction.
Diagnostics name the flooring regulation so the UI can say "floored by
ID_NICKEL_ORE_BAN".

## Stakes — what this does and does not move

Both affected pillars are at **weight 0** in the published concentration-
only score. Landing (or deferring) 067 moves **shadow scores and the
re-promotion validation record only** — the published number is untouched
until the event pillars are re-promoted. It requires **no event re-ingest**
(regulations/workbook/aggregator only). The cost of delay is that
validation evidence accumulates under the wrong classification.

## Settled by the evidence pass — please audit, don't re-derive

The 2026-08-04 evidence pass (per-measure primary sources; register in the
memo) **changed 6 of 10 floors** from the ordinal placeholder:

| regulation_key | floor | review date | one-line basis |
| --- | --- | --- | --- |
| DRC_COBALT_QUOTA_2025 | **0.55** | 2027-12-31 | caps ALL export forms at ~50% of prior volume; over-binding in practice |
| CN_MINOR_METALS_2025 | **0.50** | 2026-11-27 | worldwide licensing over near-monopolies (Ga 98.7%); proven flip-to-ban (US flows ~0.9 in 2025) |
| ZW_LITHIUM_EXPORT_BAN | **0.45** | 2027-01-01 | dominant channel (concentrate) throttled but flowing; steps to ~0.80 if the 2027 concentrate ban lands |
| ID_NICKEL_ORE_BAN | **0.35** | — | ore channel fully blocked; nickel units exit freely at record scale as NPI/matte/MHP |
| CN_REE_EXPORT_2025 | **0.30** (dual, 8 pts compliance) | 2026-11-27 | licensing that mostly approves (record 2025 exports) but real stoppages + permanent US-military zero |
| CN_DUAL_USE_EXPORT | **0.30** (dual, 6 pts compliance) | 2026-11-10 | friction not blockage; anode-controls revival → 0.55–0.65 |
| NA_UNPROCESSED_MINERALS_BAN | **0.20** | — | ore-only, concentrate exempt, enforcement demonstrably porous |
| CN_REE_MGMT_2024 | **no floor** | — | domestic production-control statute; no export mechanism; display row + events |
| CL_LITHIUM_STRATEGY | **0.00–0.05** | — | exports grew every year; SAMR condition *guarantees* supply; display row |
| MX_LITHIUM_NATIONALIZATION | **drop** | — | no production exists to restrict; events only |

Headline finding: **"ban" is not the severity.** The two outright ore bans
are the narrowest measures (each spares the dominant export channel); the
DRC "quota" is the strongest. Channel breadth × enforcement beats legal
instrument type. Compliance-side rows (UFLPA, EU Battery Reg, …) are
unchanged.

## The five decisions this review needs

1. **Calibration model** — floors ≈ (share of export flow actually
   blocked) × (enforcement effectiveness); production share deliberately
   NOT folded in because the geo pillar already multiplies exposure
   against concentration. Audit the model, not each number in isolation.
   *Question: do you accept the model? Any floor whose basis you dispute?*
2. **Dual-split magnitudes** — CN_REE_EXPORT keeps 8 compliance pts,
   CN_DUAL_USE keeps 6 (down from 20/15). Evidence supports *keeping a
   dual split* (45–120-day licences, per-shipment end-use certs, active
   2026 criminal enforcement); the magnitudes are judgment.
3. **US_WRO_HOSHINE** — kept as compliance (import-eligibility/provenance
   burden, UFLPA-family). The one genuinely ambiguous row. Confirm or move.
4. **US steel/aluminium tariffs** — would be the first
   `standing_tariff_exposure` row (Aluminum × US, in force). In the
   registry or not? (Parked since 2026-08-04.)
5. **Timing** — land with the reset #2 batch, or explicitly defer. Note:
   067 needs no re-ingest, so deferring does not force a reset #3; the
   GTA payload capture (2026-09-20) removed the API deadline from this
   decision.

## Clock awareness

Three floor review dates land in **Nov 2026** (CN suspensions expire
2026-11-10 and 2026-11-27; snap-back pushes CN minor metals toward
0.85–0.95 for US flows) and Zimbabwe's concentrate ban lands **2027-01-01**
(step to ~0.80). Whenever 067 lands, its first workbook review chore is
~7 weeks out — the floors are perishable by design, which is why
`floor_review_date` exists.

## Flagged out of scope (roadmap, not this review)

- **Destination blindness**: floors are per implementing geography; China's
  permanent US-military-end-use zero and the 2024–25 US-only ban are
  destination-specific exposures the model averages away. If partner
  customers are US-concentrated, CN floors understate their exposure.
- **Production-control factor**: CN_REE_MGMT_2024-class regimes (state
  quotas disciplining supply without restricting exports) have no home;
  natural fit is a future concentration-side factor.
- **Rollout mechanics** (two-stage rescore for attribution, editorial
  updates for reclassified rows) are specified in the design doc §5.
