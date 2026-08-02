# GTA Categorization Audit

*2026-07-31. Written before the suggestion inversion (triage plan, Phase 1) so
the logic being demoted from "hard assignment" to "suggestion" is reviewed
first. Code facts are from `app/services/ingestion/gta.py` as delivered
2026-07-31; distribution numbers were measured against the live database the
same day, after the repair re-ingest (2,629 GTA events).*

## 1. How a GTA event gets its labels today

The pipeline makes six kinds of decisions per intervention, in order. All of
them currently write authoritative values; under the triage plan all of them
become suggestions.

**Admission.** Only `gta_evaluation = Red` rows are considered (GTA's own
"harmful to foreign commercial interests" rating), and only rows whose
affected HS codes match a battery-material prefix from
`hs_code_material_mappings`. Rows failing the country gate (unresolvable
implementer) or material gate (no HS match) are skipped entirely — they never
enter the database.

**Category.** An 84-entry hardcoded map from GTA's intervention-type name to
one of three pillars: the entire export/import/FDI/trade-defense restriction
family → `geopolitical_trade`; the localisation/procurement family →
`regulatory_compliance`; the subsidy/state-aid family → `financial_pressure`.
Unknown types silently default to `geopolitical_trade`. One category per
event, no confidence attached to the mapping itself.

**Subtype.** A parallel map into six families: `EXPORT_RESTRICTION`,
`IMPORT_DISRUPTION`, `EXPORT_SUBSIDY`, `PROCUREMENT_POLICY`, `TRADE_DEFENSE`,
`FDI_RESTRICTION`. Eleven intervention-type ids (competitive devaluation, IP
protection, labour measures, "instrument unclear", etc.) are deliberately
left with subtype NULL — they still ingest and still get a category, they
just don't feed subtype-specific scoring. In practice these are rare: 4 of
2,629 stored events.

**Severity.** Base 0.9 for export bans, 0.7 for other in-force Red measures,
0.3 for lapsed ones — then multiplied by implementation-level and horizontal
modifiers and (API path) a tariff-magnitude refinement, clamped to [0, 1].

**Materials.** Every matched HS code resolves through the seeded mappings;
relevance is 0.9 × mapping confidence × a breadth discount (added Jul 15:
`min(1, 3/n)` across the event's n materials, with `is_direct = n ≤ 3`).
`match_reason = "hs_code"` is recorded on every link — provenance already
exists.

**Geography.** Implementing country is always `primary`. "Affected" rows are
written only for `IMPORT_DISRUPTION` events and only when the affected list
is short enough to be meaningfully targeted.

## 2. What the 2,629 stored events actually look like

| category | subtype | events | share |
|---|---|---|---|
| financial_pressure | EXPORT_SUBSIDY | 1,456 | 55% |
| geopolitical_trade | IMPORT_DISRUPTION | 619 | 24% |
| geopolitical_trade | EXPORT_RESTRICTION | 359 | 14% |
| geopolitical_trade | FDI_RESTRICTION | 69 | 3% |
| regulatory_compliance | PROCUREMENT_POLICY | 64 | 2% |
| geopolitical_trade | TRADE_DEFENSE | 58 | 2% |
| (subtype NULL) | — | 4 | <1% |

Top single intervention type: **Financial grant, 802 events** — nearly a
third of the entire corpus. Top implementers overall: China 651, US 486,
India 265, Japan/Canada/Austria 114 each. Material fan-out after the Jul-15
breadth work: 1,115 events tagged to one material, 499 to 2–3, but **774
events (29%) still carry 9+ material links** (weight-discounted, `is_direct
= false`, but present). 608 events (23%) are no longer in force and sit at
~0.35 average severity.

## 3. Findings

**F1 — The majority of the corpus is subsidies, and the category tells the
wrong story. This is the single highest-value fix.** 55% of events are
`EXPORT_SUBSIDY` → `financial_pressure`. GTA marks these "Red" because they
harm *foreign competitors* — but from a battery buyer's supply-risk
perspective, a US grant to a cathode plant or a Canadian loan guarantee for
a mine mostly *adds or diversifies supply*. The implementer distribution
makes the point: 586 of the subsidies are Chinese, but 496 come from the US,
Japan, Australia, Canada, and Germany — allied industrial policy (IRA-era
grants and their peers) currently being fed into a *risk* pillar as if it
were pressure. This is the same direction problem the IEA tracker fix
solved on Jul 15 (`positive_policy` derived instead of hardcoded).
Recommendation for the suggestion engine: add a **direction** field
(`restrictive | supportive | neutral`) derived from the subtype family —
restriction/defense families → restrictive, subsidy family → supportive —
and default supportive events' suggested status to `display_only` rather
than scoring. Whether supportive events should eventually *reduce* risk is
a scoring design question that stays deferred; the immediate point is to
stop suggesting them as risk.

**F2 — Import-side measures are relevance-ambiguous, but this can wait.**
The 619 `IMPORT_DISRUPTION` and 58 `TRADE_DEFENSE` events are real trade
events, but whether India's tariff on Chinese cells is a supply risk depends
entirely on whose supply chain you're scoring — the current pipeline treats
it like any other geopolitical event, differing only in base severity. The
honest fix (perspective-dependent relevance) is a scoring-layer design
problem. For the suggestion engine, carrying the existing subtype into the
UI is enough for a human to judge; nothing to change at ingest now.

**F3 — Fan-out is handled at the weight level; make it visible at the UI
level.** The breadth discount already de-weights the 774 omnibus events. The
suggestion engine should surface `n_materials`, `is_direct`, and
`match_reason` in the triage UI so a reviewer immediately sees *why* an
event carries 14 material tags and can prune with one action. No ingest
change needed — the columns already exist.

**F4 — Country anomalies to spot-check in triage, not fix in code.**
Austria as the #6 implementer (114 events, including 36 "export
restrictions") almost certainly reflects EU-wide measures recorded at
member-state level, or a jurisdiction-resolution artifact worth eyeballing.
The US as the #1 export-restriction implementer (57) is plausibly real
(export controls) but worth confirming the events are battery-relevant
rather than swept in by broad HS lists. Flag these two slices for early
review in the UI; don't guess in code.

**F5 — Lapsed measures are one-quarter of the corpus.** 608 events with
`in_force = false` ingest at 0.3-base severity and stay in evidence pools
forever (no expiry). Historically useful, but the suggestion engine should
carry `in_force` visibly, and the eventual scoring pass should decide
whether lapsed events belong in trailing windows at all. No change now;
recorded as a scoring-phase decision.

**F6 — Silent defaults should become low-confidence suggestions.** Unknown
intervention types default to `geopolitical_trade` with no marker that the
mapping was a fallback. As a hard assignment that's invisible; as a
suggestion it should carry `suggestion_confidence: low` (or equivalent) so
the UI can sort uncertain rows first. Cheap to add during the inversion.

**Leave-alones.** Severity calibration (deferred to the scoring phase per
the plan), the subtype families themselves (they carve the space cleanly),
the dedupe/identity layer (rebuilt Jul 30), and the Red-only admission
filter — though note the plan's long-term "industry feed" goal will
eventually want Amber/liberalizing measures as display-only content, which
is an *admission* question, not a categorization one.

## 4. Repair-run coverage — measured, and mostly explained

Today's successful repair re-ingest (run with a 2023 date floor) repaired
1,473 summaries and repointed 1,498 events to per-intervention permalink
documents; the offline backfill gave all 2,629 events permalink metadata.
1,075 events were untouched — and the year split shows why: 1,019 of them
(95%) are dated 2018–2022, i.e. outside the 2023 floor the run was given.
The genuine residual is **56 post-2023 events the API did not return
despite being in-window** — most plausibly date-semantics quirks (the API
filters on announcement period while `event_date` can come from
implementation or a later action) rather than systematic filter drift.

Repairing the remainder is one more run with the full window —
`--api-since 2018-01-01`, with `--api-raw-cache` so the pull happens once —
costing roughly 2,600 metered records, which is free while demo access
lasts. After that run, whatever is *still* truncated is the true
unreachable set; it should be small, and each such row can be checked
against its permalink by hand. The reset-planning rule stands in milder
form: **the Phase 4 reset only brings back what the API query asks for**,
so the reset's pull must use the full date window, and the pre-reset count
verification remains the backstop against silent narrowing.

## 5. What the suggestion engine should emit, per event

Category, subtype, direction (new — F1), severity, confidence,
material links with relevance / `is_direct` / `match_reason` / `n_materials`
(F3), `in_force` (F5), and a mapping-confidence marker for fallback
categorizations (F6) — all as suggestions on a `pending_triage` event,
none as authoritative values. Everything except direction and the
fallback marker already exists in code or schema; the inversion is mostly
re-labeling, which is what makes Phase 1 tractable.
