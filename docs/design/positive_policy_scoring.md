# Positive-Policy Scoring — Design for Partner Review

**Status:** DRAFT 2026-07-13 — layers 2–3 need partner methodology sign-off before implementation.
**Author:** Nicole + Claude working session.
**Context:** today the scoring engine is structurally positive-only. Events reach scores through
subtype allowlists (`_TARIFF_SUBTYPES`, `_EXPORT_SUBTYPES` in `hs_node_scorer.py`) feeding
`compute_event_impact()` = severity × confidence × recency × relevance, floored at zero.
There is no concept of sign anywhere. POSITIVE_POLICY events are deliberately inert
(2026-06-07 directional fix). The goal: supportive policies should eventually *reduce* risk —
without corrupting the risk semantics that exist.

---

## Two traps this design exists to avoid

**Trap 1 — axis mismatch (optimism from unbuilt capacity).**
Most supportive policies (tax credits, financing, permitting reform) act on *future supply
structure*. The risk math measures *current* concentration and disruption (HHI over actual
production shares; disruption events). Netting a policy announcement against today's HHI-driven
risk imports promises into a measurement of reality. A Canadian tax credit does not reduce DRC
cobalt concentration on the day it is announced — or possibly ever.

**Trap 2 — cross-source double-counting.**
The same physical measure appears in multiple sources (an Indonesian export ban exists in GTA
*and* the IEA tracker). Dedup is content-hash *within* a source; there is no semantic
cross-source dedup. Any wiring that lets two sources' copies of one measure both move a
sub-score is wrong in whichever direction it points.

---

## Layer 1 — Direction taxonomy at ingest (IMPLEMENTED 2026-07-13)

Every IEA event now derives `direction ∈ {supportive, restrictive, neutral}` from its
policyType names (restrictive keywords checked before supportive so "export financing
restrictions" cannot land supportive):

* `supportive` → `positive_policy: true`, POSITIVE_POLICY subtype (INVESTMENT_PLEDGE only,
  and only when direction agrees). Feed for Layer 2.
* `restrictive` → correctly labeled, **stays informational** (subtype None). GTA is the
  authoritative risk-raising source for trade measures (Trap 2).
* `neutral` → lists/stockpiling/standards machinery; rationale context only.

### Layer 1b — GTA Green (liberalising) interventions — SPECCED, NOT IMPLEMENTED

The trade-side positive signal we do not ingest at all today: GTA Green = liberalising
measures (tariff cuts, export-restriction removals). These are *current-structure* positives —
unlike subsidies they take effect on implementation, so they are the cleanest candidates for
eventual score impact.

Implementation sketch (one unknown blocks it):

1. **Verify the evaluation ID.** `_GTA_API_HARMFUL_EVAL_IDS = [4, 5]` covers the harmful
   universe. The Green ID must be read from the taxonomy endpoint — do not guess:
   `POST https://api.globaltradealert.org/api/v1/gta/mappings/` (needs the API key; blocked
   on today's rate-limit window). Record the ID as `_GTA_API_LIBERALISING_EVAL_IDS`.
2. `fetch_gta_interventions_api(..., evaluation_ids=...)` parameter; `ingest-gta
   --include-green` CLI flag (default off until partner sign-off).
3. `parse_gta_api_response` currently hard-filters `gta_evaluation == "Red"` client-side —
   extend to accept Green when the flag is set; Green events get `event_subtype =
   "POSITIVE_POLICY"`, low severity (0.2 band, mirroring IEA), `positive_policy: true`,
   `policy_direction: "supportive"`, and the standard material/HS/geography junctions.
4. Dedup note: a Green "removal of export ban X" and the original Red "export ban X" are
   different interventions with different hashes — both correctly exist. The *decay* of the
   Red event plus the Green mitigation signal is how removal shows up (see Layer 2), until
   partner decides on explicit revocation linking (GTA `state_act_id` chains could support
   it later — out of scope here).

---

## Layer 2 — Standalone mitigation signal ("policy momentum")

**Principle: surface, don't subtract.** A parallel signal computed from POSITIVE_POLICY
events through the *same* machinery as risk (decay, confidence, relevance), stored and
displayed alongside risk — never netted into it without Layer-3 evidence.

* **Grain:** (hs_mapping × geography), aggregating upward exactly like risk
  (L0 node → L1 material×geo → L2 global), so momentum is comparable at every level a risk
  score exists.
* **Computation:** `mitigation_raw = Σ compute_event_impact(sev, conf, recency, relevance)`
  over POSITIVE_POLICY events attributed to the node/geo, squashed to [0,1] with the same
  saturation used by event sub-scores. Reuses `decay.py` with a **longer half-life** than
  disruption events (policy programs act over years; a 2024 tax credit is still "on" — decay
  should reflect program life, not news recency). Proposed: 3× the disruption half-life,
  partner to calibrate.
* **Storage:** `mitigation_score` + `mitigation_rationale_json` columns on
  `hs_code_geography_risk_scores` and `material_geography_risk_scores` (one migration).
* **Display:** paired presentation — "Risk 0.72 · Policy momentum 0.41" with the event list
  in the rationale drawer. Momentum is a *leading indicator* users interpret; the platform
  does not pretend to know its conversion rate into supply.
* **Explicitly rejected for now:** `risk − k·mitigation` netting. Reason: Trap 1. If the
  partner wants a single blended number later, the blend constant `k` is a methodology
  decision with the forward-HHI evidence (Layer 3) as its justification, not a code default.

Open questions for partner:
1. Half-life for policy decay (proposal: 3× disruption).
2. Should Green (implemented liberalisations) carry higher weight than announcements?
   (Proposal: yes — severity by status: implemented 0.30 / announced 0.15, mirroring but
   exceeding the current IEA status bands, because implemented liberalisation is
   current-structure.)
3. Does momentum appear on the public score pages or analyst view only at launch?

---

## Layer 3 — Structural mitigation via forward-looking HHI (the only netting channel)

Positive policies legitimately reduce risk **where they demonstrably change supply
structure**. The platform already holds the vehicle: `facilities` with status
`construction / planned / announced` and capacity figures (partner-curated seed).

* **Forward shares:** recompute production shares per (node × country) with pipeline
  capacity added, weighted by materialization probability per status. Draft priors for
  partner calibration: `operating 1.0 · construction 0.7 · permitted 0.4 · announced 0.15`.
* **Forward HHI delta:** `ΔHHI = HHI_current − HHI_forward` per node. A positive delta =
  pipeline diversification. This is the number that can *justifiably* attenuate the
  concentration component of risk, because it is anchored in named facilities with owners,
  countries, and capacities — not policy prose.
* **Policy linkage (the honest role for Layer 1/2 data):** a policy-momentum score co-located
  with pipeline facilities (same geo × material) raises the materialization prior modestly
  (e.g. +0.10, capped); momentum with *no* pipeline facilities changes nothing. Policies
  stop being skipped exactly when, and only when, there is steel in the ground to support
  them.
* **Double-count handoff:** when a facility flips to `operating`, its capacity enters the
  *actual* share computation (facility-derived shares ETL, already on the roadmap) and must
  simultaneously leave the forward adjustment — the status flip is the single switch.
* **Surface:** `forward_hhi`, `hhi_delta`, and a `structural_mitigation` factor in score
  rationale; whether/how much `hhi_delta` attenuates the live concentration sub-score is the
  core partner decision (proposal: cap attenuation at 15% of the concentration component at
  launch).

---

## Sequencing

1. ✅ Layer 1 (IEA direction) — shipped 2026-07-13; active on the next IEA ingest.
2. Layer 1b (GTA Green) — after the rate-limit window: one mappings call to verify the eval
   ID, then ~1 session of implementation. Default-off flag until partner reviews.
3. Layer 2 — after partner answers the three open questions. One migration + one scorer
   module + display wiring.
4. Layer 3 — after facility-derived shares ETL lands (already queued); priors calibrated on
   the cobalt benchmark pilot before any attenuation goes live.

## Manual events vocabulary (added 2026-07-13)

The manual risk-events workbook used POSITIVE_POLICY as a general "positive direction —
don't score as risk" flag: 84 rows, of which 66 were favorable *company outcomes*
(settlements, acquittals, dismissed suits) rather than government policy. Split applied:

* **POSITIVE_POLICY** (18 rows kept) — government actions supportive of supply
  (TRADE_POLICY + REGULATORY_COMPLIANCE types: DoW price floor, tariff exclusions,
  permits/approvals, Corfo quota expansion). These feed Layer 2.
* **POSITIVE_DEVELOPMENT** (66 rows) — favorable company outcomes. Scoring-inert, same as
  before; a future *company-pillar* positive signal could consume these, but they must never
  enter geography×material policy momentum.

Direction on manual events stays human-audited — keyword derivation is for bulk sources
only. Partner review note: a few kept rows are borderline (DOJ monitorship ending, SAMR
merger approval are company-specific relief through a regulatory instrument) — demote
individually if the policy-momentum semantics feel stretched.

## What this deliberately does not do

No changes to `compute_event_impact` signatures, no signed severities, no reweighting of the
six supplier pillars, no cross-source semantic dedup (tracked separately). Each of those is a
bigger blast radius than the value it adds at this stage.
