# Event Triage Pipeline — Plan of Attack

*Drafted 2026-07-30. Decisions in §3 were made by Nicole in session; everything
else is proposal until reviewed. Counts in §2 were measured against the live
Neon database on 2026-07-30.*

## 0. Status ledger (updated 2026-08-03)

The plan below is kept as written; this section is what actually happened.

| Phase | Status |
| --- | --- |
| 0 — GTA quality | **Done** (2026-07-30/31, incl. repair re-ingest + backfill) |
| 1 — audit + inversion | **Done**, all active ingesters. Its open item (`needs_material_review` triage facet) shipped 2026-08-01. Never built: the "low-confidence marker on fallback categorizations" from the GTA audit — likely moot since the harmonisation sweep left zero fallbacks in the corpus, but noting it was dropped, not delivered. |
| 2 — status model (066) | **Done, applied.** Soft-dismiss dedupe wiring done (`event_dedupe` consults rejected). |
| 3 — triage web UI v1 | **Done, beyond spec.** All v1 actions (confirm-as-is, pillar change, link add/remove, all four statuses, fully bidirectional, `triaged_by`/`triaged_at` stamped), operational promote with the severity ladder, plus unplanned: duplicate hints, data-quality flags, defect facets, quality-defect chips, resolved geography names/pills, and a material-coverage tracker (confirmed vs queue-pending per material, dashboard thin-events thresholds served by the API). The country filter — the one v1 spec gap — closed 2026-08-03 via the `risk_event_geographies` junction. Still unsurfaced: per-link `n_materials` fan-out. |
| 3b — Supply Concentration view | **Not started.** |
| 4 — the reset | **Done** (Nicole, by 2026-08-03). Post-reset live counts: 2,282 events — 1,190 `pending_triage`, 1,089 `display_only` (machine-routed at ingest: supportive-direction F1 rule, AD/CVD procedural, geography aggregates), 3 `rejected`, 0 `scoring`. The September GTA-access deadline no longer gates the reset itself. |
| 5 — triage the queue | **Active phase.** 1,190 pending. Note the queue-summary "reviewed" figure counts machine-routed `display_only` rows; human decisions so far are the dismissals only. |
| 6 — scoring flip | **Not done** (by design — awaits a confirmed baseline). Nothing outside the triage API reads `triage_status`; frozen grandfather scores still displayed; scheduled scoring stays off until this flips. |
| 7 — content feed | **Not built.** Related leak closed early (2026-08-03): every event-serving read surface (`risk_events` list, intelligence entity pages, company events, dashboard KPIs, monthly chart, materials counts, market-scores evidence counts) now excludes `rejected` rows — "hidden everywhere" was previously only true on the triage surface. |

§7 open items still open: suggestion-engine quality measurement (needs a few
hundred triage decisions), and verifying the IEA re-ingest recovered the 407
truncated summaries — checkable now that the reset has run.

## 1. Why this plan exists

The strategic premise, per partner direction: the data itself is the product.
A single place where industry-relevant public-source events live — complete,
correctly attributed, and traceable to their source — is what people will pay
for. Nobody will steer a business decision off a single score for a material,
but a curated live feed of relevant events, with scores as a reflection of
what that data says, is useful and validating.

The current pipeline was built in the opposite order. Bulk ingestion assigns
labels, categories, material links, and weights *at insert time*, before any
human has judged whether an event is even related to the industry. The result
is a database where a large share of events carry machine-guessed assignments
that nobody has reviewed, descriptions are truncated mid-word, some events
have no working source link, and manually entered events are not tagged with
their actual source. Scoring built on top of that inherits every one of those
problems.

This plan inverts the pipeline: **the machine proposes, a human disposes.**
Ingesters capture clean raw events and attach *suggestions* — material links,
pillar, type, weight — but nothing becomes authoritative until it passes
through a triage queue where Nicole and her partner confirm, edit, or dismiss.
Scoring is not dismissed; it moves to the far end of the pipeline where it
consumes curated data instead of raw guesses.

## 2. What exists today (measured, not assumed)

The database holds **3,609 risk events**. By source: Global Trade Alert 2,576;
IEA Critical Minerals Policy Tracker 438; manual walkthrough 207 (all 207
`verified = true`); SEC EDGAR 144; Federal Register 101; EUR-Lex 6 (all
verified); and 137 events with **no source document at all** — sanctions-
related rows (mostly `GEOPOLITICAL_TRADE`, plus `sanctions_listing` and
`geography_sanctions_exposure` types) whose ingest path never created a
provenance record.

Known quality defects, all verified against production: 2,497 of the GTA
events and 407 of the IEA events have summaries sliced at exactly 500
characters with no ellipsis — a display cap that leaked into storage, and the
full text is not recoverable from the database because `raw_text` was never
stored. Every GTA event points at a per-run bulk-CSV document with a NULL
URL, so the events API renders them with no source link. Twelve GTA events
carry future dates (out to 2029-01-01) which, because every evidence window
is a trailing window and the decay function clamps future dates to its
*maximum* multiplier, are weighted more heavily than events happening today —
permanently. Manual walkthrough events are number-heavy rather than
editorial, and are not tagged with the actual publication they came from.

Some of the machinery this plan needs already half-exists. The
`risk_event_materials` link table already carries `relevance_score`,
`match_reason`, `is_direct`, and `scope_type` — assignment provenance is
partially built. A four-state event vocabulary — `scoring | rejected |
pending_triage | display_only` — already exists in code
(`event_dedupe._event_status`) but was never promoted to a database column.
`display_only` is exactly the "industry-relevant, viewable, but not scored"
middle state this plan needs. And the GTA quality fix (§5, Phase 0) is
already about two-thirds rebased onto the current working tree.

## 3. Decisions already made

These were made explicitly in session and are treated as settled unless
Nicole reopens them.

**Scoring during the transition: grandfather + queue new.** The last computed
scores are kept and displayed as-is. New and re-ingested events land as
`pending_triage` and do not feed scoring. Scoring re-runs only once a
confirmed baseline exists. The known cost: the displayed scores continue to
reflect un-reviewed assignments until the switch is flipped — accepted
because, in Nicole's words, the scoring still generally aligns with
expectations, so it is safe as a placeholder.

**The existing backlog: delete, re-ingest, keep old scores.** Rather than
migrating 3,609 events in place, feed-backed events are deleted and
re-ingested through the new pipeline, arriving clean and as `pending_triage`.
The frozen scores keep the UI looking close to its future state meanwhile.
§6 covers the sources that *cannot* be re-ingested and must be preserved.

**Dismissal is soft.** Rejected events keep their database row, hidden from
scoring, triage, and the content site. Deduplication consults them so the
next ingest run cannot resurrect a dismissed event as fresh. Hard deletion is
ruled out precisely because re-ingestion is now a normal, repeatable
operation.

**Triage v1 scope: category, materials, status.** The triage surface
exposes pillar (confirm or change), material links (add or remove), and
status (approve for scoring / display-only / dismiss). Event type, relevance
weights, and severity remain machine-suggested and are *visible* but not
editable in v1 — editing them is deferred to keep the first version small
enough to ship quickly.

**Review surface: straight to a web UI (revised 2026-07-31).** The earlier
"xlsx round-trip now, UI later" decision is superseded — the interim
spreadsheet flow is skipped entirely and triage ships as a web UI from the
start. Since triage doesn't begin until after the reset anyway, the xlsx
step bought nothing but a second implementation of the same state machine.

**Audit before inversion (added 2026-07-31).** The categorization logic is
reviewed and documented *before* being demoted to suggestions, so the
suggestion engine launches with known improvements rather than freezing
today's guesses. The GTA audit is done — see
`docs/design/gta_categorization_audit.md`; its headline finding is that 55%
of the corpus is subsidies mis-narrated as financial-pressure risk, and the
inversion should add a `direction` field to fix that.

Earlier decisions still in force: all ingesters stop auto-assigning
`primary_category` (the `before_insert` listener at
`app/models/regulatory.py:665` is inverted, not just GTA); and the GTA
ingest quality fix was the first deliverable (landed 2026-07-30/31,
including the repair re-ingest and offline backfill).

## 4. Target architecture

**Layer 1 — clean capture.** Ingesters store the complete source record:
full description (no storage-side truncation), a real permalink to the
specific source item, correct dates (a future implementation date is
re-anchored to its announcement date, with the forward date preserved as
`scheduled_implementation_date` metadata), and an honest source tag. Every
event gets its own `SourceDocument` row carrying the permalink. Identity is
keyed on the source's own stable identifier (e.g. the GTA intervention id),
not on a hash of mutable text, so a revised description updates the existing
row instead of duplicating it. Ingesters make **no authoritative
categorization decisions**.

**Layer 2 — the suggestion engine.** The existing assignment logic is not
deleted — it is demoted. The category map, HS-code material matching,
severity calibration, and relevance weighting all still run, but their
outputs are recorded as *suggestions*: `suggested_category` on the event,
material links written with `status = 'suggested'` and their existing
`match_reason`, suggested type and weight alongside. `primary_category`
stays NULL until a human sets it. Every event lands with
`triage_status = 'pending_triage'`.

**Layer 3 — the triage queue.** The four-state vocabulary becomes a real
column: `pending_triage` (awaiting review), `scoring` (confirmed — feeds
scoring and every display surface), `display_only` (industry-relevant and
user-viewable, but attributed to no material or pillar for scoring), and
`rejected` (soft-dismissed — hidden everywhere, retained for dedupe).
Triage decisions carry `triaged_by` and `triaged_at`. Confirming can mean
accepting the machine's suggestion unchanged — a single action in the UI —
so the queue is only as slow as the events that actually need correction.

**Layer 4 — consumption.** Scoring reads only events with
`triage_status = 'scoring'` and only *confirmed* material links — once the
switch is flipped (Phase 6). The content site reads `scoring` **plus**
`display_only`, which is what makes the feed broader than the scoring inputs:
an event can be worth showing to an industry reader without anyone having to
pretend it maps to a specific material. Until the switch flips, scoring
serves the frozen grandfather scores.

## 5. Phased workplan

**Phase 0 — finish the GTA quality work (in flight, ~two-thirds rebased).**
Summary cap 500 → 8000; identity hash keyed on `gta_id` instead of summary
text; per-intervention `SourceDocument` rows carrying verified permalinks
(`globaltradealert.org/intervention/{id}`, state-act fallback); future-date
resolution with `scheduled_implementation_date` preserved; refresh-in-place
for already-stored events (repairs truncated summaries on re-ingest, never
touches curated or scored fields); a `--dry-run` flag reporting the blast
radius before any write; the evidence-window upper bound in
`evidence_query.py`; and the materials API permalink fallback. Rebased onto
the repo's actual current files (verified by hash against the device on
2026-07-30 — the 2026-07-13 ingester work turned out to already be delivered,
and the Jul-15 breadth-discount change is preserved). Nicole runs the
re-ingest locally with her API key, dry-run first.

**Phase 1 — categorization audit, then suggestion inversion, per ingester,
GTA first.** *(GTA: implemented 2026-07-31 alongside the Phase 2 schema —
migration 066. IEA: audited and implemented 2026-07-31 — see
`docs/design/iea_categorization_audit.md`; the category-map
harmonisation sweep is DONE — 2026-07-31, decisions: subsidy family →
financial_pressure both sources, machinery → regulatory_compliance,
international arrangements → geopolitical_trade, plus export/import-control
and tariff keyword fixes; zero fallbacks remain across the stored corpus.
Open item: folding `needs_material_review` into the triage UI as a facet.
SEC EDGAR: audited 2026-07-31 and PAUSED by decision — no inversion, no
scheduled runs, excluded from the reset; see
`docs/design/sec_edgar_stream_audit.md` for the revival criteria.
Federal Register: audited and implemented 2026-07-31 — see
`docs/design/federal_register_categorization_audit.md`; AD/CVD procedural
steps demote to display_only, and for US measures FR is canonical over
GTA in the duplicate-hints UI. OpenSanctions: audited and implemented
2026-07-31 — see `docs/design/opensanctions_audit.md`; snapshot
provenance documents, first_seen-anchored dates, geography aggregates →
display_only, curation-surviving upserts. NOTE: the "137 sanctions events
with no provenance" premise was a mislabel — 119 of those are
trade-signal derived statistics (already display-only by design); only 18
are OpenSanctions events, and their provenance self-repairs on the next
run. Operational news (the 2026-07-27 candidate pipeline, not yet run in
production): harmonised 2026-07-31 onto the triage_status column —
candidates land pending_triage with suggested_category='operational',
promote → scoring, reject → rejected; its promote/reject functions become
the UI's backend for operational events, keeping the severity-ladder
semantics. **PHASE 1 IS COMPLETE** across all active ingesters.)* Each ingester's assignment logic is audited *before* being
demoted, so the suggestion engine ships with the improvements the audit
surfaces rather than freezing today's behaviour. The GTA audit is complete
(`docs/design/gta_categorization_audit.md`); its changes for the inversion:
a `direction` field (`restrictive | supportive | neutral`) derived from the
subtype family, supportive events defaulting to a `display_only` suggestion
rather than a risk-pillar assignment, a low-confidence marker on fallback
categorizations, and fan-out fields (`n_materials`, `is_direct`,
`match_reason`) surfaced for the UI. Then invert the `before_insert`
listener so nothing auto-assigns `primary_category`, and route every
ingester's assignment logic into suggestion fields. This touches GTA, IEA,
SEC EDGAR, Federal Register, and the sanctions path — the sanctions ingest
also gets fixed to create `SourceDocument` rows (correction 2026-07-31:
only 18 of the "137 no-provenance events" are OpenSanctions; the other 119
are trade-signal statistics, sourceless and display-only by design). Each
ingester gets the same audit-then-invert treatment as its turn comes.

**Phase 2 — status model migration.** *(Written 2026-07-31: migration 066,
pending `alembic upgrade head`.)* Adds `triage_status`, `suggested_category`,
`direction`, `triaged_by`, `triaged_at` to `risk_events`; `status`
(`suggested | confirmed | rejected`) to `risk_event_materials`. Grandfather
backfill: existing scoring rows → `scoring`, existing display-only rows →
`display_only`, existing material links → `confirmed` — current behaviour
is unchanged at migration time. Supportive-direction events land
`display_only` at ingest per the F1 decision. Soft-dismiss wiring into
dedupe remains open.

**Phase 3 — web UI triage flow v1 (revised 2026-07-31: xlsx step
dropped).** A queue view over `pending_triage` events — filterable by
source, suggested category, direction, material, and country — and an event
detail view showing the full summary, permalink, dates, and every
suggestion with its provenance (`match_reason`, `n_materials`, mapping
confidence). Actions in v1: confirm suggestions as-is, change pillar,
add/remove material links, and set status (scoring / display-only /
dismiss). All state moves are bidirectional (un-dismiss, un-approve), which
today's machinery has no notion of, and every action stamps `triaged_by` /
`triaged_at`. Type, weight, and severity editing deferred to v2, matching
the v1 scope decision. Operational-news candidates ride the same queue
with their own confirm flavor: promote reuses `operational_triage`'s
severity-ladder semantics (subtype → taxonomy default, explicit override,
positive subtypes refused) rather than the plain confirm policy events
get.

**Phase 3b — Supply Concentration view (added 2026-07-31, Nicole).** The
material-concentration pillar is data-derived — no events, nothing to
triage — so its transparency surface is a *display* view, built in the
same UI push. Per material: the full producer table from
`material_production_shares` (every country, share, volume, unit,
reference year, source attribution "USGS MCS <year>"), production HHI
with year-over-year trend where multiple reference years exist, USGS mine
production side-by-side with Benchmark capacity
(`material_capacity_shares`), and the criticality-signal decomposition
displayed with the exact weights scoring uses (70% HHI-blend / 30%
scarcity; 45% production HHI / 15% reserve HHI inside the blend) — the
"numbers behind the score" view. Plus one cross-material overview ranking
all 34 covered materials by concentration. Pure display of existing
tables; no schema or ingest work. Deliberate non-goal: no
"share-shifted-YoY" derived events — movement over time renders as a
chart here, not as rows in the triage queue (the statistics-as-events
pattern is quarantined, not extended).

**Phase 4 — the reset.** In order: (1) disable any scheduled scoring runs so
the frozen scores cannot be overwritten by a recompute against an empty
event table; (2) export the non-reingestable events (§6) to xlsx —
this export doubles as the moment to fix manual-event source tagging and
rewrite summaries editorially; (3) delete feed-backed events (GTA, IEA,
Federal Register, sanctions — plus the 144 SEC EDGAR orphans, which are
deleted and **not** re-ingested per the 2026-07-31 pause decision); (4)
re-ingest each active source through the new pipeline, dry-run first,
landing everything as `pending_triage` with suggestions attached; (5)
re-import the preserved manual/EUR-Lex events with their `verified` flags
and curated fields intact; (6) verify counts per source against pre-delete
numbers.

**Phase 5 — triage the queue.** Nicole and her partner work the queue in
the UI. The machine's suggestions mean most rows are confirm-or-dismiss;
the point of the exercise is the corrections, which become the curated
baseline — and, later, the reference data for improving the suggestion
engine itself.

**Phase 6 — flip scoring to curated input.** Evidence queries add
`triage_status = 'scoring'` and confirmed-links filters; a fresh scoring run
replaces the grandfathered scores. Expect scores to move — that is the
curation showing up, not a regression — but diff the before/after grid and
review anything that moves sharply before publishing.

**Phase 7 — content-site feed.** An endpoint serving `scoring` +
`display_only` events, newest first, with full summaries and permalinks. The
"more live feed" goal. Web triage UI follows as its own effort, replacing
the xlsx round-trip with the same state machine.

## 6. The reset's sharp edges

**Manual walkthrough events (207, all verified) and EUR-Lex (6, verified)
have no feed to re-ingest from.** Deleting them destroys them permanently.
They are excluded from deletion — or exported, improved, and re-imported —
but never dropped. The Phase 4 export is the right moment to fix their two
known problems (missing real source tags; number-heavy rather than editorial
summaries) since every row is passing through human hands anyway.

**The no-provenance events — RESOLVED (2026-07-31).** The "137 sanctions
events" premise was a mislabel: 119 are trade-signal derived statistics
(sourceless and display-only by migration-062 design — they are
regenerated by the trade-signal builder, not preserved); only 18 are
OpenSanctions events. The OpenSanctions ingester has true upsert on
stable keys and now creates snapshot provenance documents, so its events
are fully feed-backed: they can be deleted and re-ingested at the reset,
and the historical NULL-provenance rows self-repair on the next run
regardless. See `docs/design/opensanctions_audit.md`.

**The frozen scores must actually stay frozen.** If any scheduled job or
deploy hook triggers a scoring run between deletion and Phase 6, the grid
recomputes against an empty or pending-only event table and the UI goes
blank. Disabling automated scoring is step 1 of Phase 4, not an
afterthought, and re-enabling it is part of Phase 6.

**Re-ingest completeness is not guaranteed — for GTA, now measured and
mostly benign.** The 2026-07-31 repair re-ingest left 1,075 events
untouched, but 95% of those were simply outside the run's 2023 date floor;
the true residual is ~56 in-window events the API didn't return (likely
announcement-vs-implementation date semantics — see the audit §4). The
rule stands: a delete-and-re-ingest only brings back what the API query
asks for. Nicole's call (2026-07-31): pre-2023 data is not needed, so the
reset's pull deliberately uses a 2023 floor and the ~1,019 pre-2023 GTA
events are dropped by choice, not by accident. One consequence to note:
`financial_pressure` has no evidence window, so old subsidy events
currently feed it indefinitely — moot once supportive events route to
display_only, but worth remembering if any pre-2023 restrictive events
matter to a no-window pillar. The per-source count verification in Phase
4 step 6 remains the backstop because a silent shortfall looks exactly
like success. IEA, SEC EDGAR, and Federal Register paths need the same
check. Timing note: the full re-pulls the reset requires are free while
GTA demo API access lasts (through September 2026) — completing Phases 1–3
before then avoids paying Max-tier months for the reset.

**Curated values survive by construction, not by care.** The refresh logic
never writes `verified`, `primary_category`, `severity_score`,
`confidence_score`, or `risk_categories_json`, and the regulations table
(including the workbook-verified regulations) is not part of this plan at
all — no phase touches it.

## 7. Open items

Whether the sanctions path is re-runnable (§6) — needs investigation before
Phase 4. Whether IEA re-ingest recovers the truncated 407 summaries (its
feed must still serve the full text; unverified). How "industry-relevant"
is defined for `display_only` events with no material links — v1 answer is
"whatever survives triage," with filter rules emerging from the curated
pattern later, which is consistent with deferring weighting until a data
pattern is established. And the suggestion engine's quality itself: once a
few hundred triage decisions exist, the confirm/correct/dismiss rates per
source and per match_reason become measurable, which is how the engine gets
better instead of staying frozen at today's guesswork.
