# Stage-ladder coverage matrix — absent stages & fill potential

*Drafted 2026-08-10 from the post-refresh audit (as-of 2026-08-10), the
benchmark workbook (74 rows), USGS MCS 2026 stage coverage, and the IEA GCMO
2026. Analysis memo only — no code, workbook, or mapping changes. Companion
to the A2 freshness audit: freshness catches STALE stages; nothing catches
ABSENT ones, because a stage with no rows contributes zero silently and the
understatement banner has no would-be score to compare against. This matrix
is the coverage-complete instrument for that blind spot.*

Cell vocabulary: **P-fresh** (present, in scoring) · **P-stale** (present,
gated out) · **A-src** (absent, named source available) · **A-nosrc**
(absent, no known reputable source) · **n/a** (stage doesn't exist for this
material's physical ladder, or out of product scope).

## Launch list matrix

| Material | ore | concentrate | intermediate | refined | battery_grade | Binding today |
|---|---|---|---|---|---|---|
| Aluminum | P-fresh (bauxite, USGS) | n/a | P-fresh (alumina, USGS) | P-fresh (smelter, USGS) | n/a (foil out of scope) | intermediate 71.67 |
| Cobalt | P-fresh (USGS) | n/a | P-fresh (IEA proxy) | P-fresh (IEA) | **P-stale** (sulfate @2022) | ore 88.33 |
| Copper | P-fresh (USGS; mine≈concentrate merged at 2603) | merged | n/a (blister unsourced, minor) | P-fresh (USGS) | **A-nosrc** (battery foil — CN-heavy, no reputable share table in hand) | refined 48.73 |
| Iron Ore | P-fresh (USGS) | n/a | n/a | n/a (product scope = ore) | n/a | ore 32.78 |
| Lithium | P-fresh (USGS) | **A-src** (spodumene ≈ ore distribution, GCMO p.351 raw materials) | n/a | n/a (chemicals ARE the conversion) | P-fresh (IEA) | battery_grade 86.83 |
| Manganese | P-fresh (USGS) | n/a | n/a | **A-src** (EMM — CN ~95%+; GCMO "China processed 70–95%" family, IMnI) | P-fresh (HPMSM, GCMO) | battery_grade 99.49 |
| Natural Graphite | P-fresh (flake, USGS) | n/a | **A-src** (uncoated spherical — ~all-CN; GCMO natural-spherical series) | n/a | P-fresh (combined anode, IEA) | battery_grade 99.29 |
| **Nickel** | P-fresh (USGS) | n/a | **A-src** (MHP/matte "convertible feedstock", GCMO p.154 — ID-dominant) | P-fresh (IEA) | **A-src** (SULPHATE — see §1) | ore 77.55 |
| Phosphate | P-fresh (USGS) | n/a | **A-nosrc** (merchant WPA — no share table in hand) | n/a | P-fresh (PPA, GCMO) | battery_grade 81.35 |
| **REE (aggregate)** | P-fresh (USGS) | n/a | n/a | **A-src** (metal-making — see §2) | P-fresh (separation, IEA) | battery_grade 93.81 |

## Ranked findings — by potential to change a published number

### 1. Nickel battery-grade (sulphate) — WOULD TAKE THE HEADLINE. Decide before bands.

Nickel publishes 77.55 off Indonesian ore. Nickel **sulphate** — the actual
battery precursor — is China-dominated: GCMO p.154 "Concentration in the
battery-grade nickel market" shows CN ≈ 75% of 2025 sulphate capacity, and
the prose has China "expected to remain the dominant supplier of nickel
sulphate." Rough math: CN ~0.75 share, stage HHI ~0.58 → cliff ~0.95 → sub
≈ 82 raw; with CN's amplifier (~+5) ≈ **mid/high-80s — nickel's headline
would move ~77.6 → ~87 and its driving geo would flip ID → CN.**

Caveats before filling: (a) the GCMO chart is **capacity**, not production —
our share basis is production; a production-basis source (Benchmark nickel
sulphate assessment, or partner sourcing) is the clean fill, with the GCMO
as corroboration only; (b) the chart is category-lumped (ID/CN/RoW).
**Decision required: fill before A7, or defer explicitly with a note in
A5/A6 that nickel's published score excludes the sulphate chokepoint.**
Silent deferral is the one wrong option — this is the known largest
invisible understatement on the launch list.

### 2. REE aggregate refined (metal-making) — would raise 93.81 → ~97. Trivially fillable, needs a semantics sign-off.

The Nd/Pr/Dy/Tb children already carry refined-stage rows @2025 (280530,
CN 0.89, Rare Earth Exchanges / Mining.com — audited, loaded). The REE
**aggregate** has no refined stage. Adding the same distribution to the
aggregate: HHI ~0.80 → cliff ~0.97 → sub ≈ 92 raw > current 88.9 —
**refined would bind and the aggregate headline rises ~+3.5.** Fill cost is
near zero (reuse the children's rows/source). The sign-off needed is
ladder semantics: 280530 metal physically follows oxide separation, but is
mapped `refined` (below `battery_grade`) per the July decision — stage-max
doesn't care about order, but the methodology page must explain it, and it
should be a deliberate choice, not an artifact (partner).

### 3. Completeness fills — headline-neutral, strengthen the story

- **Natural Graphite spherical (intermediate)**: ~all-CN step; headline
  99.29 barely moves. Fills the ladder at its most concentrated point.
- **Manganese EMM (refined)**: CN ~95%+; headline already 99.49. Easy
  source (IMnI / GCMO strategic-minerals section).
- **Lithium concentrate**: ≈ the ore distribution (AU-led); would never
  bind. Lowest value.

### 4. Partner sourcing list — absent with no strong source in hand

Copper battery foil (CN-heavy, would plausibly out-score refined 48.73 if
real — flagging for sourcing precisely because it COULD bind), Phosphate
merchant WPA, Synthetic Graphite upstream (needle coke / graphitization —
acquisition plan §4.6, still unsourced).

### 5. Explicit non-gaps (so the matrix reads honestly)

Cobalt's ladder is complete — sulfate is STALE, not absent (already
banner-tracked; refresh path = CI Market Report 2025). Aluminum, Iron Ore,
Copper-ex-foil are complete for product scope. Byproduct metals (Bi, Ga,
Ge, In, Te, Se) lack ore stages **by nature** — n/a, not gaps. Rhenium and
Sodium are ladder-retag decisions (Boron-pattern), tracked separately.

## Sequencing consequence for Workstream A

A7 bands should be cut against a distribution we intend to stand behind.
Findings 1 and 2 both RAISE launch-list scores if filled. Options:

- **(a) Fill first** (nickel sulphate needs a production-basis source;
  REE refined is immediate) → re-export → cut bands once. Slower, cleanest.
- **(b) Cut bands now, defer fills with explicit notes** in A5/A6 naming
  nickel-sulphate and REE-metal as known exclusions with re-score triggers.
  Faster, honest, but bands may need a touch-up when fills land.

Recommendation: (b) for nickel (its clean source isn't in hand), (a) for
REE (the fill is a workbook edit away). Either way the choice is Nicole +
partner's, recorded here.
