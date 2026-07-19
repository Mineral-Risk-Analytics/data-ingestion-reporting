# World Mining Data 2026 — Full-Read Extraction & Integration Assessment

**Read in full 2026-07-18** (methodology, group definitions, group tables 6.3, country tables 6.4, share/HHI tables 6.5; skipped: mineral-fuels detail and the 6.6 by-country re-pivot of the same data).
**Source:** Austrian Federal Ministry of Finance, *World Mining Data 2026*, Vol. 41 (Reichl & Schatz), Vienna, April 2026. Published annually for 41 years; data deadline Feb 28, 2026 → **2024 actuals**. Free. **Statistical data downloadable in Excel from world-mining-data.info** — ingest from the Excel, never parse the PDF.

---

## 1. What this source is (and is not)

**Is:** the most complete free *mine-stage* production dataset: 65 commodities × all producer countries × 1984–2024 (tables show 2020–2024), reported as **recoverable content** (Co, Ni, Li₂O, REO, Cr₂O₃, TiO₂, W, V, P₂O₅, K₂O) — matching our contained-metal ore-stage convention. Data from World Mining Congress national-committee questionnaires + national statistics, cross-checked against USGS/BGS; **every country-row carries a source + accuracy code** (1=reported/2=estimated/3=provisional × source letter a/b/e/n/…), which maps directly onto our per-row confidence discipline.

**Is not:** a refining/midstream source. One number per commodity per country — no stage split, no facility granularity, ~14-month lag at worst (annual). It does **not** replace the benchmark workbooks (CI/Benchmark) for intermediate/refined/battery-grade shares. Two useful quirks: *Aluminium* is smelter (i.e. refined-stage) production with Bauxite separate, and *Lithium is Li₂O content* — convert ×2.153 for LCE, never mix bases with our existing tables.

## 2. The groups (what makes this source different)

For **every commodity**, chapter 6.3 gives 2020–2024 production split four ways:

1. **Development status** (UN): developed / developing / least-developed.
2. **World Bank income group**: high / upper-middle / lower-middle / low.
3. **WGI political-stability class** — the Kaufmann-Kraay-Mastruzzi indicator we already ingest per-country, bucketed: stable (≥ +1.25) / fair (0…+1.25) / unstable (0…−1.25) / extreme-unstable (≤ −1.25).
4. **Economic blocs**: OECD, EC, G7, EFTA, USMCA, BRICS, SCO, ASEAN, MERCOSUR, ACP, SADC.

And chapter 6.5 gives ready-made **2024 share-of-world tables with per-country HHI contributions and a total country-concentration HHI** — `(mod)HHI(ct)` — per commodity.

### Derived indicators these unlock (computed from the 2024 tables)

**Share of world production from unstable + extreme-unstable jurisdictions:**

| Material | % unstable+extreme | Notes |
|---|---|---|
| Graphite | **98.9%** | CN=unstable class |
| Cobalt | **94.6%** | 74.5% from ONE extreme-unstable country (CD) |
| REE | **92.4%** | incl. Myanmar 9.9% extreme-unstable |
| Nickel | **88.3%** | ID=unstable |
| Manganese | 82.3% | |
| Copper | 64.9% | most diversified base metal |
| **Lithium** | **39.6%** | the only battery mineral majority-produced in "fair" jurisdictions (AU/CL) |

(Essentially **zero** world production of any battery mineral comes from "stable"-class countries in 2024 — a striking, display-ready fact.)

**Bloc exposure (2024):** REE: SCO 68.9% vs OECD 16.5%. Graphite: SCO 83.8% vs OECD 2.8%. Nickel: ASEAN 71.8%. Cobalt: ACP 78.1% / SADC 76.3% / OECD 4.3%. Lithium: OECD 60.7% — again the outlier.

## 3. Key 2024 numbers extracted (battery-relevant, mine stage, content basis)

**Cobalt** (world 268,755 t, +34.8% y/y; HHI 5,706): CD 74.51 · ID 11.50 · RU 3.20 · AU 1.78 · CA 1.32 · CU 1.30 · PH 1.13 · PG 0.98 · MG 0.94 · CN 0.74 · NC 0.60 · ZM 0.52 · MA 0.48 · FI 0.38. Five-year CD series: 86.6k → 93.1k → 115.4k → 140.1k → 200.3k t.

**Nickel** (3.70 Mt; HHI 4,039): ID 62.17 · PH 9.57 · RU 5.87 · CA 3.58 · NC 3.12 · CN 2.99 · AU 2.68. (AU collapsed 149k→99k in 2024 — the C&M wave in our events.)

**Lithium, Li₂O** (601,914 t, +20.4%; HHI 2,442): AU 38.21 · **CN 21.05** · CL 20.48 · ZW 8.56 · AR 4.93 · BR 4.32 · CA 1.71. WMD's CN share is far above USGS narrative (domestic lepidolite counted) — worth a deliberate source decision.

**REE, REO** (398,008 t; HHI 4,861): CN 67.84 · US 11.43 · MM 9.90 · AU 5.05 · MY 1.29 · LA 1.04 · NG 1.01 · MG 1.01.

**Graphite** (1.69 Mt; HHI 5,861): CN 75.93 · IN 6.90 · MG 4.93 · BR 3.10 · TZ 2.46 · MZ 2.07.

**Manganese** (18.8 Mt; HHI 2,221): ZA 39.16 · GA 22.10 · AU 7.96 · GH 7.72 · IN 6.01 · CN 4.58. **Chromium** (HHI 3,340): ZA 54.77 · KZ 11.91. **Fluorspar** (HHI 4,884): CN 67.74 · MX 15.45 · MN 5.74. **Phosphate P₂O₅** (HHI 2,327): CN 44.07 · MA 14.59 · RU 7.72 · US 7.05. **Copper** (HHI 1,089 — unconcentrated): CL 24.03 · CD 13.53 · PE 11.94. **Aluminium smelter** (HHI 3,719): CN 59.96. Also ready: Mo (CN 41.5, HHI 2,355), W (CN 77.9, HHI 6,146), V (CN 68.5, HHI 5,127), Nb (BR 92.5, HHI 8,580), Ta (CD 41.1, HHI 2,409), Ti (CN 36.2, HHI 1,987), Sb (CN 32.7 with Myanmar surging to 18.6, HHI 2,129), Ga (CN 98.7, HHI 9,739), Ge (CN 93.3, HHI 8,726), Bi (CN 81.6, HHI 6,764), Sn (HHI 1,349), Zn (HHI 1,429), Bauxite (GN 34.2, HHI 2,106).

## 4. Cross-check against our loaded shares (validation value)

| | Our DB (USGS 2026, ore) | WMD 2024 | Verdict |
|---|---|---|---|
| Cobalt CD | 75.29% | 74.51% | ✔ agrees |
| Cobalt ID | **14.40%** | **11.50%** | ✗ 2.9pp gap — WMD has ID at 30.9kt vs USGS-implied ~36.6kt; investigate which vintage/definition |
| Cobalt RU | 2.52% | 3.20% | ~ ok |
| Cobalt HHI (raw) | 0.589 | 0.571 | ✔ same cliff tier either way |

Systematic use: WMD becomes the **independent cross-check** on every ore-stage share row we load. Divergence > ~2pp → flag for manual review. This is exactly the audit trail the spec's data-honesty principle wants.

## 5. Recommended integration (in order of value/effort)

1. **Cross-check layer (cheap, now).** One-time script comparing WMD 2024 vs loaded ore-stage shares for our materials; flag divergences (cobalt-ID already found). No schema change.
2. **Material-page display facts (V1-compatible, high product value).** "Supply-base profile" per material: % by stability class, % by bloc (OECD/BRICS+SCO), 5-yr production trend, WMD HHI with prior-year comparison. These are *informative* facts — they fit the informative-first posture and require no scoring change. The stability-mix table in §2 is the demo.
3. **Onboarding accelerant (spec §9 step 1-2).** For materials beyond the battery core (Mn, Cr, Mo, W, V, Nb, Ta, Ti…), WMD provides instant credible ore-stage shares + HHI with per-row source codes — one loader run instead of a benchmark-doc hunt. Downstream stages still need benchmark docs; WMD only fills ore.
4. **Later, scoring evidence (V1.1 candidate, decide then).** "% of production from unstable/extreme-unstable jurisdictions" is a legitimate material-level risk signal (it's the production-weighted WGI aggregate — the same math as our per-geo WGI overlay, aggregated to material level). If adopted, it belongs at **L2 as material context**, NOT double-counted into per-geo scores where the WGI overlay already lives.
5. **Source policy decision needed (one question):** for ore-stage shares, USGS stays canonical and WMD validates — or WMD becomes canonical where its vintage is fresher? Recommend: **USGS canonical, WMD validates**, single-source-per-stage rule preserved; exceptions only after a per-material review (lithium-CN is the case where they genuinely disagree on definition).

## 6. Caveats

Mine/first-processing stage only; annual with ~4–14-month lag (fine for structural signal); some rows estimated (code 2) or provisional (code 3) — carry the codes into any load; lithium in Li₂O (convert deliberately); graphite is natural-total (no flake/amorphous split); their "political stability" classes are derivable from our own WGI ingest — the *pre-aggregated per-commodity series* is what saves work; publication says "chapters 1–5 accessible" for the PDF — the **Excel on their homepage is the machine-readable route**.
