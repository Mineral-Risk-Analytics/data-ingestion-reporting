# Standing-Measure Floor Calibration — Evidence Memo

*2026-08-04. Companion to `regulation_pillar_reassignment.md` §4. That doc's
original floor table was an ordinal placeholder; Nicole asked for evidence.
This memo is the evidence pass: USGS production shares computed from the
repo's own MCS data (`data/usgs/2025/MCS2025_World_Data.csv`, PROD_EST_2024
against world totals), plus per-measure web research from reputable sources
(Reuters, FT/Bloomberg via aggregators, USGS, IEA policy database, CSIS,
Fastmarkets, Benchmark, S&P Global, ICSID filings, law-firm export-control
alerts). Items the research could not verify are marked UNVERIFIED in the
per-measure notes. **The evidence CHANGED the answer for 6 of 10 rows** —
the original table's biggest errors are called out explicitly.*

## Calibration model

`floor ≈ (share of the material's export flow actually blocked by the
measure) × (enforcement effectiveness)`, expressed on [0,1] where 1.0 = a
total, fully-enforced block of all exports of the material from the
implementing country. Production share of world supply does NOT scale the
floor — the geo pillar already multiplies exposure against concentration —
but it is listed for context because it decides how much the floor matters.

## Production shares (computed from the repo's USGS MCS 2025 data, est. 2024)

| pair | share of world mine production |
| --- | --- |
| Nickel × ID | 59.5% |
| Cobalt × CD | 75.9% |
| REE × CN | 69.2% |
| Graphite × CN | 79.4% |
| Gallium × CN | 98.7% |
| Tungsten × CN | 82.7% |
| Antimony × CN | 60.0% |
| Lithium × CL | 20.4% |
| Lithium × ZW | 9.2% |
| Lithium × NA | 1.1% |
| Lithium × MX | no production row (≈0) |

## Revised floors — evidence-based

| regulation_key | old proposal | **revised floor** | review date | one-line basis |
| --- | --- | --- | --- | --- |
| ID_NICKEL_ORE_BAN | 0.80 | **0.35** | — | Ore channel ~totally blocked and tightly enforced, but nickel exits freely at world-record scale as NPI/matte/MHP — total nickel exports are unrestricted |
| DRC_COBALT_QUOTA_2025 | 0.55 | **0.55** | 2027-12-31 | Quota caps ALL export forms at ~50% of prior volume; over-binding in practice (exports ran below quota); regime runs through 2027 |
| ZW_LITHIUM_EXPORT_BAN | 0.80 | **0.45** | **2027-01-01** | Ore banned but concentrate (the dominant channel) flows under licence/quota at growing volume; steps to ~0.80 if the Jan-2027 concentrate ban lands with only one sulphate plant built |
| NA_UNPROCESSED_MINERALS_BAN | 0.70 | **0.20** | — | Ore-only; concentrate explicitly exempt (all formal producers unaffected); enforcement demonstrably porous (ministry permitted the very shipments police had stopped) |
| CN_REE_EXPORT_2025 | 0.65 | **0.30** | **2026-11-27** | Licensing, not a ban: 2025 total REE exports hit a record (+12.9%); but real 2-month stoppage, permanent zero for US-military end-use, US-bound yttrium −95%, 45–120-day licences; suspended Oct-2025 escalation snaps back Nov 2026 |
| CN_MINOR_METALS_2025 | 0.60 | **0.50** | **2026-11-27** | Worldwide licensing over near-monopoly metals (Ga 98.7%) with proven flip-to-ban capability (US ban Dec 2024–Nov 2025 was ~0.9 for US flows); currently suspended, not repealed |
| CN_DUAL_USE_EXPORT | 0.55 | **0.30** | **2026-11-10** | Standing licensing chassis; graphite licences routine after the 2024 rollout shock (−65/−78% for two months); anode-material controls suspended to Nov 2026 — revival would justify 0.55–0.65 |
| CN_REE_MGMT_2024 | 0.50 | **NO FLOOR** | — | ~90% domestic production-control statute (state ownership, quotas, traceability); contains no export mechanism — exports rose to record levels under it. Does not belong in an export floor at all |
| CL_LITHIUM_STRATEGY | 0.25 | **0.00–0.05** | — | Ownership restructuring; JV sealed Dec 2025; production +10% in 2025, SQM record sales; the only trade condition found (China SAMR) *guarantees* continued supply. Does not belong in an export floor |
| MX_LITHIUM_NATIONALIZATION | 0.20 | **0.00 — drop standing weights** | — | Mexico produces no lithium; there is no flow to restrict. ICSID arbitration active, no award. Investment-climate/future-supply risk — events only |

## What the evidence overturned

**The original table's worst error was treating "ban" as the severity.** The
two outright bans (Indonesia 0.80→0.35, Namibia 0.70→0.20) turned out to be
the *narrowest* measures — each blocks only the raw-ore form while the
country's dominant export channel flows freely — while the DRC's mere
"quota" caps every form of the material at half its prior volume and is the
strongest measure on the continent. Channel breadth × enforcement beats
legal instrument type.

**The Zimbabwe/Indonesia inversion.** The placeholder had them equal at
0.80. Evidence puts Zimbabwe *above* Indonesia (0.45 vs 0.35): Zimbabwe
throttles its dominant channel (concentrate: licence-gated, 10% export tax,
approved-large-producers-only, briefly banned outright in Feb 2026);
Indonesia's ban doesn't touch its dominant channel at all.

**Two rows don't belong in the floor.** China's 2024 REE Management
Regulations are a domestic production-control law — the research verdict is
~90% domestic character with no export mechanism; representing it as export
exposure would be a category error (its natural home is a future
production-control/concentration-side factor — same family as the quota
data the concentration pillar already reads). Chile and Mexico are
ownership regimes: Chile's exports grew every year under the strategy and
Mexico has nothing to restrict. All three stay in the registry as display
rows (their events still matter) with no standing arithmetic.

**Floors are perishable — the design needs a review date.** Three Chinese
suspensions expire 2026-11-10/27 (snap-back would push CN minor metals to
~0.85–0.95 for US-destined flows and dual-use toward 0.55–0.65), and
Zimbabwe's concentrate ban lands 2027-01-01 (step to ~0.80 if unslipped —
industry is lobbying for a delay; the one sulphate plant operational
against 1.1 Mt/yr of concentrate says the deadline binds hard if enforced).
`floor_review_date` is added to the schema in the design doc so these
cliffs surface as workbook chores instead of silent staleness.

## Known model limitation surfaced by the research

The floor is per (material × implementing geography); it has no
*destination* dimension. China's US-military-end-use prohibition is a
permanent 1.0 for that narrow channel, and the Dec 2024–Nov 2025 period was
~0.9 for US-destined Ga/Ge/Sb while worldwide flows continued — a
destination-differentiated exposure the current model averages away. Noted
as out of scope; if partner customers are predominantly US-exposed, this
understates their true exposure to the Chinese regimes and a
destination-aware exposure model is the eventual fix.

## Dual-split validation

The proposed compliance-side residue for the two `dual` rows (CN_REE_EXPORT
8 pts, CN_DUAL_USE 6 pts) is supported: documented licence timelines of
45–120 days, per-shipment end-use certification demands on foreign buyers,
and active criminal enforcement (2026 detentions, whistleblower mechanism)
are a real ongoing compliance burden on anyone buying from China,
independent of whether shipments ultimately flow.

## Source register (primary citations per measure)

Full inline citations live in the research transcripts; keys per measure:

- **Indonesia**: IEA policy 16084; USITC EBOT "Nickel in Indonesia"; Argus
  (2026 RKAB quota cut); SCMP (KPK ~5.3 Mt smuggling finding); Katadata
  (94% of H1-2025 nickel exports to China).
- **DRC**: IEA policy 28969; ARECOMS Decision 001/2025 + Press Release
  2026/003 (via SMM); Benchmark; Fastmarkets (quota ≈50% of 2024 exports;
  export-vs-quota shortfall; Zambia concentrate-route closure); Africanews
  (Dec 2025 resumption).
- **Zimbabwe**: SI 213/2022 (via McCarthy Tétrault); S&P Global (2027
  concentrate ban announcement); Al Jazeera (Feb 2026 blanket suspension;
  Jun 2026 boom coverage incl. Q1-2026 exports +27%); Miningmx/Benchmark
  (Apr 2026 conditional lifting); Mining Zimbabwe (Bikita licence, Huayou
  sulphate first-export).
- **Namibia**: IEA policy 28970; GTA intervention 120443; Reuters/Mining
  Weekly (police stop order); The Namibian (30,000 t permit weeks later);
  Global Witness / London Politica (governance).
- **CN REE (both)**: gov.cn (State Council regulations); Reuters (secret
  2025 quotas; record 2025 exports 62,585 t; general licences Dec 2025);
  MOFCOM Announcements 18/2025, 46/2024, 55–62, 70, 72 (via Clark Hill,
  White & Case, Pillsbury); CSIS "One Year Later" (US yttrium 333 t→17 t);
  USGS MCS 2026 heavy-REE.
- **CN minor metals / dual-use**: USITC EBOT Ga/Ge; Reuters (Thailand/
  Mexico antimony transshipment, ~3,834 t); PricePedia (antimony $59,750/t
  record); USGS MCS 2026 tungsten; Fastmarkets (graphite −65/−78% rollout
  shock); Morgan Lewis (2026 enforcement: Dalian detentions, whistleblower
  Announcement 26/2026).
- **Chile**: Codelco press releases (NovaAndino JV sealed 27 Dec 2025);
  MINING.COM/Bloomberg (SAMR supply condition); Mining Technology/
  GlobalData (2025 output +10.1%); Rio Times 2026 guide (CEOLs, Rio Tinto
  Maricunga).
- **Mexico**: ICSID ARB/24/21 filings (PO4/PO5 — suspension denied twice,
  no award); Mexico News Daily (settlement talks); Rio Times (LitioMx
  2026–30 program, no capex budget).
