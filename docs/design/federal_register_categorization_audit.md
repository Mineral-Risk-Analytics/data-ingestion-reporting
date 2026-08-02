# Federal Register Categorization Audit

*2026-07-31, fourth in the per-ingester audit series. Decisions made by
Nicole in session: demote AD/CVD procedural steps to display_only, and
apply the Phase 1 suggestion inversion in the same pass. Both implemented.*

## What it ingests and how it categorizes

US-only, keyless, same-day primary source. Seven targeted query groups,
each with a **hardcoded** classification — there is no keyword-map
guessing here, which makes FR the second ingester (after SEC's item map)
whose categorization is structural rather than inferred: OFAC sanctions
and BIS export controls → `EXPORT_RESTRICTION` / geopolitical_trade;
ITA/USITC/USTR/CBP trade-remedy actions → `TARIFF` / geopolitical_trade;
presidential trade documents (five term-filtered streams for tariff,
§232, §301, critical minerals, sanctions) → `TARIFF`/`TRADE_POLICY`;
USGS/BLM/MSHA mining regulation and UFLPA Entity List → 
`REGULATORY_COMPLIANCE`. Priority routing dedupes documents claimed by
multiple queries. Severity is document-type-based (final rule 0.70,
presidential 0.80, notice 0.40) plus capped keyword bumps. Material
attribution uses FR's editor-curated `topics` vocabulary (treated as
authoritative, reasonably), title regex, and Haiku full-text for opaque
documents — 128 links across 101 events, no runaway fan-out.

## Findings

**F1 — Procedural exhaust was the dominant output. RESOLVED.** 97 of 101
stored events are `TARIFF`, and most are administrative steps of ongoing
AD/CVD cases — postponements, preliminary results, court notices,
supplemental schedules — each stored as a separate ~0.54-severity event.
The Türkiye aluminum-sheet case produced four events in one week. Decision:
initiations and final determinations remain real `pending_triage` events;
intermediate steps are detected by a conservative title-pattern list and
land `display_only` with severity capped at 0.25 and
`triage_route = auto_display_only_procedural`. A title the patterns miss
stays a full event — false negatives cost a redundant triage row, never a
lost measure.

**F2 — Overlap with GTA runs in the opposite direction from IEA's.** These
are the same underlying US AD/CVD measures GTA records as trade-defense
interventions — but FR is the *primary* source (same-day, legal text,
document permalink) and GTA the curated secondary that arrives later. So
FR is the canonical stream for US measures, and the triage UI's
cross-source duplicate hints should default the *GTA* copy to
duplicate-of the FR canonical for US actions — the mirror of the
IEA-restrictive recommendation. No code change; a UI-phase note.

**F3 — Direction is uniform.** Every FR stream captures restriction-side
measures; there is no subsidy stream. `direction = "restrictive"` on all
events. If a supportive stream is ever added (e.g. DOE funding notices),
it must carry its own direction rather than inheriting this default.

**Leave-alones.** The query-group design (agency-scoped + denylist + Haiku
policies, redesigned 2026-05-12) is the most deliberate admission gate of
any ingester; the topic-vocabulary material attribution; the severity
ladder for substantive documents.

## What the inversion changed (implemented 2026-07-31)

`suggested_category` (first category of the query config, geopolitical
first where multi-tagged) and `direction` written as columns;
`primary_category` never set; procedural steps → `display_only` with
capped severity per F1; `category_mapping: "query_config"` distinguishes
these structurally-derived suggestions from keyword-derived ones in the
UI; `is_procedural_step` recorded on every event; material links written
with `status = 'suggested'`. First ingest-level test coverage for this
module: 12 tests over procedural detection and the inversion fields.
