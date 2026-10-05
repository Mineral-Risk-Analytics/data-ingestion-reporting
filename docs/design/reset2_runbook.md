# Reset #2 Runbook — delete + re-ingest with the accumulated fix batch

*Drafted 2026-08-18; updated 2026-09-24. Supersedes the reset procedure
implied by the retired `REINGEST_PLAYBOOK.md` (001→034 era) for EVENT
sources; regulations, shares, and scores are untouched by this reset.
Companion: triage plan §5 Phase 4 + §6 sharp edges.*

*2026-09-24 status: the September GTA free-window deadline NO LONGER
GATES this reset — the full raw payload was captured to a local cache on
2026-09-20 and the replay path is validated, so the GTA re-ingest is free
forever (see §2.6). ALL §1 decisions are now resolved. The remaining
pre-flight item is one fresh IEA CSV export (§2.4). The remaining clock
is triage investment: every triage session done BEFORE the reset adds
decisions the delete destroys — reset first, triage after.*

## ✅ EXECUTED — closure record (run 2026-09-28/29, closed 2026-10-01)

**Final counts** (diff vs `reset2_before.json`, snapshot 2026-09-24):
Global Trade Alert 1498 → **1590** (+92, exactly as predicted: 1,626 kept
from the cache minus 15 unknown-country and 21 no-material gate skips;
replayed from `data/gta_api_raw_since2023_20260920.json`, ZERO API
records consumed). Federal Register 98 → **60** — NOT a shortfall: the
full `--since 2020-01-01 --max-pages 20` backfill ran; the drop is the
current stricter pipeline (Q5 denylist ×6, Haiku off-scope ×653)
reclassifying what the pre-audit pipeline had admitted. IEA 436 → **438**
(correct Critical Minerals dataset). Preserved sources byte-stable:
manual_walkthrough 272, EUR-Lex 6, Operational news 19, OpenSanctions 18.

**Verification, all green:** gta 144300 (Egypt sand duty) reads
**2024-02-21** — fix #4 confirmed corpus-wide. Unknown-geography-code
query: **0 bad links** on 5,275 (was 2) — fix #6 confirmed, Jersey
seeded. IEA summaries diagnostic: no cap-truncation signature in any
source. The two counts-diff "VERIFIED ROWS LOST" flags are the ACCEPTED
loss (§1), fully covered by the 22-row export
`reset2_manual_triage_decisions.txt` (written 2026-09-24, pre-delete) —
re-apply via triage UI by gta_id / FR title.

**Deviations worth remembering:**
1. The first IEA ingest used a wrong-dataset export (the general
   Policies view — 502 records, Buildings-topic tags, 4-title overlap
   with the real dataset → 294 junk events). Caught by the counts diff;
   repaired by IEA-only delete + re-ingest of the correct export
   (`policies-929.csv`, 667 records, all Critical Minerals). LESSON:
   validate an IEA export (record count ~670, avg description ~1.5k,
   topics all Critical Minerals) BEFORE ingesting.
2. The Critical Minerals dataset was byte-identical to the 2026-07-13
   export — the IEA's 2026-07-16 site update touched other datasets.
3. The FR ≈98 expectation in §3 was wrong — it assumed the old
   pipeline's admission rate. 60 under current gates is the real number.

**Residual steps outside this doc:** re-apply the 22 triage decisions;
un-pause weekly ingest jobs. Post-reset posture per §5: triage decisions
are durable from here; this corpus is the D2 evidence baseline and the
C3/C4 content-feed substrate.

## 0. What this reset applies (the accumulated fix batch)

Fixes already in the ingesters that only reach the stored corpus via
re-ingest (no-backfill rule):

| # | Fix | Landed |
|---|---|---|
| 1 | EU-collapse jurisdiction resolution (list[0]→Austria) | 2026-08-04 |
| 2 | Export-ban severity respects `in_force` (0.9/0.3) | 2026-08-04 |
| 3 | HS 7204 → Iron Ore mapping + scrap keywords (steel-scrap measures admitted/attributed) | 2026-08-18 |
| 4 | GTA `latest_action_date` override removed — event dates anchor to the ORIGINAL announcement/implementation (94 corpus rows currently amendment-dated, 21 by >1 yr) | 2026-08-18 |
| 5 | Severity staleness by design (insert-only refresh) — re-ingest is the repair | standing |
| 6 | Geography-code validation: 2-letter tokens must be seeded countries; XJ→CN subdivision remap; Jersey (JE) added to seed. Corpus impact: exactly 2 links (1 XJ, 1 JE — audit 2026-09-24) | 2026-09-24 |

Verifications closed: IEA summaries clean (2026-08-18); GTA revision-date
suspicion confirmed → fix #4; unknown-geography-code audit → fix #6.

NOT in this batch (needs no re-ingest): the 067 pillar reassignment —
schema, loader, aggregator floor, and workbook values all landed
2026-09-22/24 and touch regulations only. Its partner audit (the amber
`verified=FALSE` rows) is non-blocking for the reset.

## 1. Blocked-on decisions — ALL RESOLVED except OpenSanctions

- [x] **Pillar reassignment (067 + floors).** RESOLVED 2026-09-22/24:
      implemented in full (migration 067, loader validation, aggregator
      floor term, evidence-memo values loaded). Decoupled from the reset —
      it requires no event re-ingest. Partner audit of the floor values is
      pending but non-blocking (both affected pillars are weight 0).
- [x] **XJ region codes.** RESOLVED 2026-09-24 (fix #6). The 2 corpus
      links repair via the replay.
- [x] **Operational-news events:** RESOLVED — EXCLUDE from the delete.
      14 of its 19 rows carry human decisions incl. 3 scoring promotions,
      and Google News RSS items cannot be re-pulled.
- [x] **Triage/duplicate decisions since 2026-08-03:** RESOLVED — measured
      2026-09-20: 15 human decisions total (9 GTA, 6 FR; zero duplicate
      marks on feed rows; IEA/OS zero). Accept the loss; export the 15 to
      `reset2_manual_triage_decisions.txt` first (keyed on gta_id /
      document number) and re-apply by hand post-reset. No preservation
      tooling — 15 rows doesn't justify it. (If a FUTURE defect ever
      forces another re-ingest after serious triage investment, build the
      stable-key preserve/re-apply step then; content_hash + gta_id are
      the stable keys.)
- [x] **OpenSanctions in or out:** RESOLVED 2026-09-24 (Nicole) — **OUT**.
      Its rows are company-anchored and don't contribute to material-level
      event scoring, and the company-events workstream hasn't started.
      The 18 existing rows are PRESERVED (not deleted, not re-ingested);
      OS re-ingest waits for the severity-ladder rework / company events.

## 2. Pre-flight (any time before reset day)

1. `python scripts/diagnose_iea_summaries.py` — re-confirm clean. ✔ 2026-08-18
2. `seed-hs-mappings` — 7204 Iron Ore mapping upserted. ✔ 2026-09-20
   (ran BEFORE the GTA capture, so scrap measures are in the cache)
3. `seed-countries` — Jersey row (fix #6). Run once after the 2026-09-24
   commit if not already done.
4. Fresh IEA Policy Tracker CSV export (their tool). **STILL OUTSTANDING —
   and genuinely needed**: the newest export on disk
   (`data/iea/crit_min_policies.csv`) is dated 2026-07-13, three days
   BEFORE the tracker's last update (2026-07-16, checked by Nicole
   2026-09-24). Re-ingesting from the on-disk file would miss that
   update. Confirm the fresh CSV serves FULL summaries (spot-check one
   long policy text).
5. Load the 65 report-derived manual rows. ✔ 2026-09-22 (dedupe
   protection in place before the GTA replay lands overlapping measures)
6. **GTA raw cache** ✔ captured 2026-09-20:
   `data/gta_api_raw_since2023_20260920.json` — 2,231 interventions,
   346 MB, 2023-01-01 floor, pulled AFTER seed-hs-mappings. Replay
   validated at full scale 2026-09-20 (dry-run: 1,626 rows kept post-
   filter; 1,492 skipped-existing + 6 refreshed == the 1,498-row corpus
   exactly; 92 would-be inserts). **BACK THIS FILE UP off-disk** — it is
   a paid-records point-in-time asset; with it, this reset and any future
   GTA re-ingest cost zero API records.
7. Export the 15 human triage decisions (§1 snippet) →
   `reset2_manual_triage_decisions.txt`.
8. Pause weekly ingest jobs (GTA/FR/opnews fire Sundays 20:00–22:30) for
   the reset window so a cron pull doesn't interleave with the manual
   sequence.

## 3. Reset day — ordered steps

1. **Snapshot counts:**
   `python scripts/diagnose_source_counts.py snapshot --out reset2_before.json`
2. **Export triage decisions** (§2.7) if not already done.
3. **Delete feed-backed events** for: Global Trade Alert, IEA Critical
   Minerals Policy Tracker, Federal Register API.
   DO NOT delete: manual_walkthrough, EUR-Lex, Operational news watchlist,
   OpenSanctions (both decided: excluded/preserved). SEC EDGAR orphans
   were already removed in reset #1 and stay out.
4. **Re-ingest, dry-run first, in this order:**
   - GTA **from the cache** (no API call, no metered records):
     `ingest-gta --use-api --since-year 2023 --api-raw-cache
     data/gta_api_raw_since2023_20260920.json --dry-run`, review counts
     (expect ≈1,590 inserts on the empty table — the 1,626 kept rows
     minus unknown-country/no-material gate skips), then the real run.
   - IEA from the fresh CSV (§2.4).
   - Federal Register.
5. **Verify:**
   - `python scripts/diagnose_source_counts.py diff --against reset2_before.json`
     — preserved sources unchanged; feed sources within expected deltas
     (GTA: ≈ +92 vs pre-reset from newly admitted 7204 scrap measures and
     new-since-last-pull rows; 94 rows should show older event dates than
     before — spot-check a few from `gta_date_drift.csv`, e.g. gta 144300
     Egypt sand duty should now read 2024-02-21).
   - Unknown-geography-code query (the §1 XJ audit snippet) returns ZERO
     bad links.
   - `python scripts/diagnose_iea_summaries.py` — still clean.
   - Machine-routing sanity: supportive-direction rows landed display_only;
     AD/CVD procedural display_only; queue size plausible vs reset #1's
     1,190.
   - Re-apply the exported triage decisions (15 rows, by gta_id / FR
     document number).
6. **Un-pause the weekly ingest jobs.**

## 4. Explicitly NOT in scope

Scores and the concentration pillar (the 5.0 cutover is a separate,
manual, diff-reviewed rescore — it can run before or after this reset
with identical published results, since events carry weight 0);
regulations table and workbooks (incl. all 067 changes); benchmark
shares; company/facility seeds. The frozen 4.4 grid stays archived for
the cutover diff regardless of this reset.

## 5. Post-reset

Triage restarts on the clean queue (manual-workbook facet available) —
and from here triage decisions are meant to be durable: scoring changes
never touch events, suggestion/dedupe improvements never overwrite human
decisions, and a future ingest defect is handled by targeted re-ingest
plus (if volume warrants by then) the stable-key preserve/re-apply tool.
Duplicate-review work per the agreed plan: manual/report-row pairs first;
feed-pair sweeps only now, after the reset, so pair decisions stick.
D2 re-promotion evidence accumulation begins from this corpus.
C3/C4 content-feed work debuts on this corpus.
