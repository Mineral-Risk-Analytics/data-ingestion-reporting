# A7 — Band recalibration proposal (concentration-only distribution)

*Drafted 2026-08-11 from the final pre-bands audit export (as-of 2026-08-10,
all launch binding stages at 2025 data). This is the decision document for
Nicole + partner sign-off — the last open decision in Workstream A. No code
changes until approved; implementation scope at the end.*

## 1. Why the current bands are broken

30/50/65 was calibrated against the 4.3 five-pillar distribution (7 CRIT /
13 HIGH / 11 MOD / 9 LOW). Concentration-only scores skew structurally
higher — one concentrated stage is enough to set the number, and most
battery-relevant midstream is China-concentrated. Under 30/50/65 today:

- Launch list: **8 CRIT / 0 HIGH / 2 MOD / 0 LOW** — CRIT means nothing.
- Corpus (37 scored): 30 CRIT (81%). A rating where 8 in 10 materials are
  "critical" ranks nothing and undermines the defensibility story.

Bands are absolute cuts by design (keep that property — comparability over
time beats a forced curve). The cuts must move because the *scale* changed,
not to engineer a pretty distribution.

## 2. The distribution being banded (2026-08-10 final)

Launch list, sorted: Iron Ore 32.78 · Copper 48.73 · Aluminum 71.67 ·
Phosphate 81.35 · Lithium 86.83 · Nickel 87.55 · Cobalt 90.01 · REE 96.70 ·
Natural Graphite 99.29 · Manganese 99.49.

Structural features that matter for cut placement:

- A genuine low tail exists now (Iron Ore, and corpus: Silver 16.0, Tin
  20.1, Zinc 23.6, Zirconium 28.5) — the denominator fixes created it.
- A **tight cluster at 86.8–90.0** (Li, Ni, Co) — any cut inside 85–90
  splits materials separated by ~3 points. No cut avoids this entirely;
  the choice is which side of the cluster the top band starts.
- A hard ceiling cohort ≥94.7 (nine corpus materials 94.7–100.0, incl.
  launch REE/NG/Mn) that any defensible CRIT must contain.

## 3. Options

**Option A — 35 / 60 / 85.** Launch: 1 LOW / 1 MOD / 2 HIGH / 6 CRIT.
Corpus: 5 / 5 / 8 / 20 (54% CRIT). Lithium (86.83) enters CRIT by 1.8
points. CRIT stays crowded — better than today, still top-heavy.

**Option B — 35 / 60 / 90 (recommended).** Launch: 1 LOW / 1 MOD /
4 HIGH (Al, Phosphate, Li, Ni) / 4 CRIT (Co, REE, NG, Mn). Corpus:
5 / 5 / 12 / 16 (43% CRIT). CRIT is reserved for materials where the
binding chokepoint is ~80%+ single-country — the tier reads as "no
meaningful alternative supply exists," which is what CRIT should claim.

**Boundary case to decide explicitly, not discover later:** Cobalt lands
CRIT at 90.01 — one hundredth above the cut. Two honest readings: (i) it
belongs in CRIT regardless — its stale battery-grade stage (would-be 89.4)
and the DRC quota mean the published 90.01 *understates* known risk, so the
boundary placement is conservative in the right direction; (ii) if partner
prefers boundary distance, an 88 cut yields identical memberships with
cobalt 2 points clear — at the cost of a less legible round number. I
recommend 90 with reading (i) recorded, because the understatement banner
is on-page evidence for the CRIT placement.

**Not proposed:** percentile/forced-curve bands (breaks absolute-cut
comparability); keeping 30/50/65 (see §1); moving the lower cuts further
(35 and 60 both fall in natural gaps: 32.8→37.2 and 56.5→60.5 in the
corpus — cuts in gaps minimize churn from small data revisions).

## 4. Sensitivity / churn check

Materials within ±2 points of a proposed cut (the revision-risk set):
- 35: Iron Ore 32.78 (LOW side; 2.2 clear). Molybdenum 37.16 (MOD side).
- 60: Tantalum 60.51 (HIGH side, 0.5 clear — flag: a small revision flips
  it; acceptable for a non-launch material, worth a tooltip disclosure).
- 85 (Option A): Lithium 86.83, Vanadium 85.98 — two near-boundary.
- 90 (Option B): Cobalt 90.01, Tungsten 90.05 (CRIT side); Nickel 87.55,
  Tellurium 89.51 (HIGH side). The 85–90 cluster is fragile under EITHER
  option; Option B at least makes the top tier's meaning strict.

Annual refresh churn is bounded: the 2025-data vintage holds until the
2027 GCMO/MCS cycle (stale cliff 2028-01-01), so banded memberships are
stable for the launch window barring source revisions.

## 5. What approval unlocks (implementation scope, pre-agreed files)

1. `app/services/scoring/bands.py` — the three cut constants + comment
   recording this calibration (distribution date, decision, sign-offs).
2. `lib/utils/risk-band.ts` — the frontend mirror (scoreToBand).
3. Copy sweep: regulation/editorial pages that name band thresholds or
   CRIT membership (B4 list); ScoreChip legend if it prints cuts.
4. A5/A6 reference the approved cuts; the cutover diff re-runs with them.

## 6. Sign-off ledger

- [x] Nicole — option: **B (35/60/90)**, date: 2026-08-11
- [ ] Partner — option: ____, date:
- Notes / amendments: Cobalt boundary reading (i) accepted — 90.01 CRIT
  stands, understatement banner is the on-page evidence. Implemented
  2026-08-11 in `app/services/scoring/bands.py` +
  `lib/utils/risk-band.ts` (same-commit pact) with boundary tests in
  `tests/scoring/test_bands.py`; copy sweep touched only
  `components/hub/Sidebar.tsx` (stale "25/45/60" comment — dashboard
  page and score-badge already derive bands from `scoreToBand` with no
  inline thresholds; server-side banding flows through `_band_out` →
  `score_to_band`, so it inherited the new cuts with no change).
  Partner sign-off remains open; cuts are live pending it per Nicole's
  go-ahead.
