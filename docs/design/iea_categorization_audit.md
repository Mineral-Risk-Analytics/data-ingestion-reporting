# IEA Policy Tracker Categorization Audit

*2026-07-31, following the GTA audit (`gta_categorization_audit.md`) —
same purpose: review the assignment logic before it is demoted to
suggestions, so the suggestion engine ships with known improvements.
Distribution numbers measured against the live database (438 IEA events)
the same day. The Phase 1 inversion for this ingester was implemented
alongside this audit.*

## 1. How an IEA event gets its labels today

The IEA ingester is in materially better shape than GTA was, because two of
the hard problems were already solved here first: the **direction taxonomy**
(2026-07-13: `supportive | restrictive | neutral` derived from policyType
names, replacing a hardcoded `positive_policy=true` on every event) and the
**needs-review flag** (2026-05-12: events with no confident material
attribution are preserved and marked `needs_material_review` instead of
silently dropped — a proto-triage queue that predates the triage plan).

The decisions per event: **admission** — rows need a title and a country;
empty-material events pass only if a battery-relevance gate (keyword or
CRM-categorical policyType) fires. **Category** — a 14-keyword scan of
policyType names: investment/financing/subsidy/trade/procurement/strategic →
`geopolitical_trade`; recycling/standards/reporting/regulation/milestone →
`regulatory_compliance`; default `regulatory_compliance`. **Direction** —
restrictive keywords checked before supportive so export-control families
can't land supportive. **Event type / subtype** — investment-family events
become `INVESTMENT_PLEDGE` with subtype `POSITIVE_POLICY` (which routes
nowhere in scoring — deliberate, 2026-06-07); restrictive events stay
subtype-NULL so they can't double-count GTA's authoritative export-control
coverage. **Severity** — status-based, 0.10–0.20; these events barely move
scores by construction. **Materials** — tech-basket inference at 0.40
relevance plus DB-keyword scan, refined by the optional Haiku classifier,
with the same breadth discount as GTA.

## 2. What the 438 stored events look like

By direction: **supportive 209 (48%), neutral 206 (47%), restrictive 23
(5%)**. By category: `regulatory_compliance` 211, `geopolitical_trade` 227.
104 events carry the `POSITIVE_POLICY` subtype. **196 events (45%) are
flagged `needs_material_review`** — no confident material attribution.
Average severity 0.193.

## 3. Findings

**F1 — Direction was already solved; the inversion just wires it to
routing.** Nearly half the corpus is supportive policy. Under the F1
decision (2026-07-31) these now land `triage_status = display_only` at
ingest — consistent with GTA. Implemented.

**F2 — RESOLVED (2026-07-31): the category maps are harmonised.** The
sweep ran the full production corpus through the categorization functions
and surfaced two outright bugs on top of the known inconsistency: IEA's
export/import controls and tariffs had **no keyword at all** — the DRC
cobalt suspension, China's antimony/REE controls, and Tanzania's lithium
ban all fell through to the default and suggested `regulatory_compliance`
while GTA maps identical measures to `geopolitical_trade`; and the
direction keywords missed import-side controls, so "Import controls and
restrictions; Minerals Recycling" derived *supportive* via the recycling
tag. Decisions (Nicole, 2026-07-31): subsidy/financing family →
`financial_pressure` across both sources (GTA's convention; IEA changes);
neutral machinery (strategic plans/lists, stockpiling, geological surveys)
→ `regulatory_compliance`, retiring the bare-"strategic" keyword;
International arrangements → `geopolitical_trade` explicitly. The map is
now ordered so trade measures outrank subsidy tags in combos, and FDI
keeps GTA's geopolitical convention despite containing "investment".
Projected distribution over the 438 stored events under the new map:
regulatory_compliance 202, geopolitical_trade 121, financial_pressure 115
— and **zero remaining fallbacks** (every production policy-type combo now
maps explicitly).

**F3 — folded into F2's resolution** (the "strategic" over-reach was
retired by the machinery decision).

**F4 — The taxonomy comment overstates one guarantee.** The 07-13 comment
claims "export financing restrictions" cannot land supportive; in fact the
restrictive keywords are exact substrings ("export restriction") and that
interleaved phrase would match 'financ' → supportive. Not a real IEA
policyType name, so no production impact — but the new test suite documents
the actual behaviour (`test_known_gap_compound_phrase_not_caught`) so the
gap is visible rather than assumed away.

**F5 — `needs_material_review` should fold into the triage UI, not remain a
parallel queue.** 196 events already sit in a metadata-flag review queue
that predates the triage plan. The UI should treat `needs_material_review`
as a filter facet on `pending_triage` rather than a separate surface —
otherwise there are two review queues with different semantics.

**F6 — Restrictive IEA copies: recommend display-only at triage.** The code
already refuses to give the 23 restrictive events a scoring subtype because
GTA is the authoritative source for trade measures and cross-source dedupe
doesn't exist — yet pre-inversion they still entered pillar pools via
`primary_category`, contradicting the code's own reasoning. They now land
`pending_triage`; the *recommendation* when triaging them is display-only
unless GTA demonstrably missed the measure. Left as pending rather than
auto-routed because that's a per-event judgment, unlike the categorical
subsidy case.

**Leave-alones.** The direction taxonomy itself, the battery-relevance
admission gate, tech-basket relevance at 0.40, the status-based severity
ladder, and the Haiku refinement path — all deliberate, documented, and
consistent with the deferred-weighting posture.

## 4. What the inversion changed (implemented 2026-07-31)

`suggested_category` and `direction` written as columns; `primary_category`
never set (events cannot score until confirmed); supportive events land
`display_only` with `triage_route` recorded; `category_mapping:
explicit|fallback` marks silent defaults (with a helper that detects
fallback by match, not by value — several keywords map to the same category
as the default); `n_materials` stamped; material links written with
`status = 'suggested'`. First-ever test coverage for this ingester: 9 tests
across the direction/category helpers and the ingest path.
