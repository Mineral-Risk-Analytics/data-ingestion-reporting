# Benchmark Share Acquisition Plan — Downstream Stage Coverage

**Started 2026-07-18, updated 2026-07-19.** Context: V1 concentration = max across FRESH stages, but for most launch materials only the ore stage had loaded shares — so CN's (and ID's) refining/battery-grade chokeholds were invisible to scoring. Cobalt was fixed first via the CI pilot. This plan tracks filling the downstream (intermediate / refined / battery-grade) shares for the rest.

**Single workbook: `benchmark_shares_v3.xlsx` (34 rows).** All benchmark rows live here now — do not spin up new versioned files, add rows to this one. Rows:
- **Cobalt (11 rows, `audited=Y`)** — CI/Benchmark, already the loaded pilot.
- **IEA Data Explorer (17 rows, `audited=N`)** — Lithium / Nickel / Graphite ×2 / REE, ref 2024.
- **GCMO 2026 report (6 rows, `audited=N`)** — Manganese HPMSM + Phosphate PPA, ref 2025.

Nothing new loads until you flip `audited=Y` per row (loader gate). Original `benchmark_shares.xlsx` (11 cobalt) is superseded by v3.

---

## 0. DONE this cycle

- **Manganese + Phosphate battery-grade — DRAFTED (2026-07-19, from GCMO 2026).** The two launch materials that were ore-only now have a battery-grade row each: Manganese sulphate/HPMSM → node `283329` (CN .95); Phosphate PPA → node `280920` (CN .70). Rows 30-35 in v3. (§1, §2.6)
- **REE stage — DONE.** HS `284690` reclassified intermediate→battery_grade in live DB + `seed_hs_mappings.py`, for the REE aggregate **and** the four element materials (Nd/Pr/Dy/Tb). REE row loads as drafted; CN .913 becomes the driving stage. (§2.4)
- **Graphite — RESOLVED: combined-anode shared stage.** Battery-grade graphite = one converged market shared by both Natural and Synthetic (both carry CN ~95.7% at battery_grade); upstream stays material-specific. **Independently validated by the GCMO 2026 annex** (its "refined battery-grade" table lists Japan/Indonesia/Germany/Korea — synthetic-only producers — alongside China at 94%). (§2.3, §3)
- **Lithium — settled, no change.** battery_grade placement confirmed correct. (§2.1)
- **Copper cross-check — root-caused + tabled.** The IEA CN 44 vs 52 spread confirmed the legacy `material_production_shares` table is a stage-less mine/refinery hybrid (copper CN 33.7% vs true mine CL 26.5). The global rollup's geopolitical weighting was repointed off it to the hs ore stage; migrating the material-page API off it is a separate tabled display fix. (§3)
- **GCMO 2026 report skimmed** → `gcmo_2026_reading_map.md` (sections that fill gaps, deep-read targets, strategic-minor-minerals). Confirmed the IEA CM Data Explorer's six minerals are the only ones with clean country tables; Mn/phosphate come from report narrative.

---

## 1. What's in the workbook (downstream rows)

| Material | Stage → HS nodes | Rows (share) | Sum | Source |
|---|---|---|---|---|
| Lithium | battery_grade → `2825;282520;2836;283691` | CN .702 · CL .204 · AR .055 · AU .018 | .979 | IEA DX 2024 |
| Nickel | refined → `7502;750210` | ID .429 · CN .313 · RU .036 · CA .032 · AU .023 · FI .018 | .851 | IEA DX 2024 |
| Natural Graphite | battery_grade → `380110;380130` (combined-anode) | CN .957 · JP .024 | .981 | IEA DX 2024 |
| Synthetic Graphite | battery_grade → `380110` (combined-anode) | CN .957 · JP .024 | .981 | IEA DX 2024 |
| Rare Earth Elements | battery_grade → `2846;284690` | CN .913 · MY .045 · US .012 | .970 | IEA DX 2024 |
| **Manganese** | **battery_grade → `283329` (HPMSM)** | **CN .95 · JP .03 · BE .02** | **1.00** | **GCMO 2026 (p.188)** |
| **Phosphate** | **battery_grade → `280920` (PPA)** | **CN .70 · MA .05 · US .05** | **.80** | **GCMO 2026 (p.190)** |

IEA DX rows are computed over IEA's full-universe Total row (no dropped-tail renorm). Expected scoring effect once loaded: Li / graphite / REE / Mn / Phosphate all gain a battery-grade driving stage anchored on CN dominance; nickel gains an ID-led refined stage.

## 2. Basis caveats — read before flipping `audited=Y`

1. **Lithium taxonomy.** IEA "refining" = lithium chemicals (carbonate + hydroxide) = our **battery_grade** nodes; `refined` (2805 metal) is tiny and irrelevant. Deliberate mapping, not an error.
2. **Nickel basis is wide.** IEA "refining" includes NPI and class II — that is why ID leads at .429. Class-I-only would look CN/RU/CA-heavier. Refinement needs INSG (§4).
3. **Graphite = combined natural+synthetic anode (shared stage).** IEA's battery-grade graphite figure blends both chains; both material records carry it, labelled "combined-anode basis". Both graphite materials therefore score near-identically at concentration — differentiation lives upstream. Split into material-specific rows only if a natural-vs-synthetic anode source lands (§4).
4. **REE is magnet-REE only** (Nd/Pr/Dy/Tb). Loads to battery_grade after the 284690 reclassification (both `2846` and `284690` now battery_grade). Not total-REO refining.
5. **IEA DX vintage.** GCMO 2025 base case, 2024 column = estimate, not census. Fine for benchmark-grade; defer to a primary trade/industry number where one exists.
6. **Manganese + Phosphate — China share IEA-stated, non-China tail ESTIMATED.** Unlike the IEA DX rows, these come from the GCMO 2026 *report narrative* (China >95% Mn sulphate p.188; China 70% PPA p.190), not a country table — the annex has clean tables only for the six key minerals, not Mn/phosphate. So the CN concentration is solid; the small tail (JP/BE for Mn; MA/US for PPA) is estimated from named ex-China players and should get your eye. Phosphate node sums to .80 (~20% "other" left unlisted, none individually large in 2025).

## 3. Cross-checks (confidence + spreads)

- **Graphite combined-anode CONFIRMED** — GCMO 2026 annex "refined battery-grade supply" 2025: China 2139 / world 2266 = 94.4%, with Japan/Indonesia/Germany/Korea (no natural-graphite mines) in the tail → proves the figure is combined natural+synthetic. Top-3 = 99%.
- **Cobalt refining** — CI/Benchmark CN .786 (loaded) vs GCMO 2026 annex CN 180/238 = 75.6% (all-forms basis). Both mid-to-high 70s; difference is basis (final refined products vs all forms). No change.
- **Nickel refining** — v3 IEA DX ID .429 / CN .313 (2024) vs GCMO 2026 annex ID 45.1 / CN 31.1 (2025). Consistent; annex is fresher if we want to bump the row.
- **Copper refining** — IEA 44 vs legacy table 52; confirmed legacy `material_production_shares` is contaminated (copper shows CN 33.7% vs true mine CL 26.5). Rollup bypassed it; material-page migration TABLED.
- **Graphite mining spread** — IEA 85.5 / USGS 79.9 / WMD 75.9; basis ladder, logged for the ore-stage review (doesn't affect the battery_grade rows).

## 4. WHAT'S LEFT

### 4a. Immediate — your action
- **Audit + load v3.** Review the 23 `audited=N` rows (incl. the Mn/Phosphate tail estimates), flip `audited=Y`, `seed-benchmark-shares "...v3.xlsx" --dry-run`, load, rescore. Fills Li/Ni/graphite/REE/Mn/Phosphate downstream in one pass.

### 4b. Acquisition — remaining source hunts (priority order)
1. **Nickel intermediate (matte/MHP) + class-I split — INSG.** IEA's wide refining basis blurs the ID→CN midstream chokepoint. INSG gives class I/II + intermediates. Paid-ish; check citable-free excerpts.
2. **Synthetic graphite UPSTREAM — needle coke + graphitization.** Synthetic's real chokepoints: needle coke feedstock (Phillips 66/US, Seadrift-GrafTech/US, ENEOS & Mitsubishi/Japan; coal-tar-pitch increasingly CN) + graphitization (overwhelmingly CN). No clean source: USGS doesn't cover it; HS 271312 is noisy (fuel-grade coke dominates). Sources: Benchmark/Fastmarkets, GrafTech/Phillips 66 10-Ks, China export-control events (Dec-2023 licensing, Oct-2025 anode expansion). High product value (needle coke is a known bottleneck).
3. **REE finished magnets (850511/850519).** Separated-oxide stage now covered; magnet *fabrication* (~90%+ CN) needs the 8505 Comtrade re-pull (already on backlog) or Adamas Intelligence.
4. **Graphite natural-vs-synthetic anode split.** To replace the shared combined figure with two material-specific rows: Benchmark/Fastmarkets anode data or USGS graphite MYB.
5. **Manganese + Phosphate tail refinement.** China share is solid; if the tail matters, a full country table would come from Benchmark (Mn sulphate outlook), the EU CRM study, or company disclosures (Euro Manganese/Black Canyon; Prayon/OCP/ICL). Also optional: swap our USGS ore stage for GCMO mining shares (Mn: SA ~40/GA 25/GH 10/AU 8; phosphate rock CN 45/MA<15/US 10) — cross-check only, USGS stays canonical.

### 4c. GCMO 2026 deep-read targets (from the reading map)
- **pp.20-37 Mineral market trends** — consolidated refining-concentration-by-material + prices (the cross-material refining data).
- **Nickel / Graphite / REE outlooks (pp.147 / 163 / 174)** — per-material midstream detail + 2035 projections for our thin stages.
- **pp.38-57 Geopolitical developments** — China Oct-2025 export controls (cathode, precursors, anode, equipment), DRC cobalt quota, Zimbabwe/Mozambique — feeds the **events + regulatory** workbooks, not shares.
- **pp.198-212 Strategic minor minerals** — non-launch onboarding (gallium ~98%, tungsten ~78%, tellurium/germanium/indium/antimony ~70-76%, titanium ~68%) + IEA's own risk methodology and a ready risk ranking to benchmark our scores against.

### 4d. Note
WMD stays out of this plan: ore-stage only, role fixed as crosscheck/QA, never a scoring input. Demand data (IEA demand sheets, import shares) is deliberately out of scope — V1 scores supply only.

## 5. Load flow (unchanged)
1. Review v3's `audited=N` rows, edit anything, flip `audited=Y` on rows you accept.
2. `uv run bdi-ingest seed-benchmark-shares "Automotive Data Solutions/benchmark_shares_v3.xlsx" --dry-run` — check per-node-year sums + row report.
3. Load (drop `--dry-run`), then per-material rescores (Lithium, Nickel, Natural/Synthetic Graphite, REE, Manganese, Phosphate) — or fold into the full Railway rescore run.
4. After rescore, sanity-check `driving_stage` in `material_geography_risk_scores.rationale` — expect battery_grade to take over for all six.
