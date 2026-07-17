# Concentration Coverage Gap Analysis — Launch Materials

**Date:** 2026-07-15 · **Status:** Bridging queue for partner review
**Companion to:** `concentration_share_weighting.md` (Fix A + Fix B sqrt + participation
gate + L1 purity filter, all implemented 2026-07-15, rescore pending)

## Why this matters now

The L1 purity filter makes `hs_code_production_shares` the SOLE source of the
Material Concentration pillar. Event-only L0 rows no longer masquerade as
concentration signal — which was correct to remove, but it means every node
without share data is now a true blind spot rather than a noisy one. Concretely:
China's ~65–75% grip on refined cobalt contributes **nothing** to CN×Cobalt L1
concentration today, because our only cobalt share data is at the ore stage
(where CN holds 0.65%). The fixes removed wrong signal; this queue adds the
missing true signal.

Stage weights make the blind spots expensive in exactly the wrong order:
battery_grade carries 0.30 of the rollup, refined 0.25, intermediate 0.20 —
ore, where nearly all our coverage lives, carries only 0.10.

## Method

Inventory of all 316 global-scope HS mappings (2026-07-15, live DB): share-country
count, linked-event count, export trade value (4-digit family), facility presence.
Launch-11 materials only; scrap/fabricated stages excluded (not in the rollup).
Trade data is used as a **prioritization signal only** — never as a share source:

* Comtrade coverage in our DB is partial and skewed (TR heavily over-represented;
  reporter set incomplete), and stored at 4-digit aggregation.
* 4-digit families are multi-material baskets: 2836 "carbonates" is mostly soda
  ash, not Li₂CO₃ (TR's 46% of family exports is not lithium); 2833 "sulfates" is
  mostly not CoSO₄; DE's large numbers everywhere are substantially entrepôt
  re-exports.
* Bridging therefore requires **production/capacity benchmarks**, transcribed to
  the seed workbook and loaded via the idempotent loader (queue already exists in
  project memory).

Note: several midstream nodes carry `market_scope='us'` import-source shares
(e.g. 750210 CA 0.44, 2825 CL 0.54). These answer "where do US imports come
from," not "who produces globally" — they must NOT be copied into global scope.

## Tier 1 — L1 concentration blind above ore, high stage weight, high event pressure

**1. Cobalt midstream + battery grade — worst gap in the platform.**
Nodes: 2822/282200 (oxides/hydroxides), 8105/810520/810530 (mattes, unwrought),
2833-side CoSO₄, 291529 (acetates). Zero global shares on ALL of them; 400–600
linked events each; family exports $37B combined. Export evidence confirms what's
missing: CD is 75% of family 2822 exports (hydroxide), and CN's refined dominance
is invisible at L1. FI (Kokkola, ~10% of world refined) is share-anchored only at
ore — the gate correctly won't rescue it midstream.
**Sources:** Cobalt Institute annual statistics (production by country, by form);
IEA Global Critical Minerals Outlook refining tables; USGS MCS cobalt-refinery
text. Pilot already queued in project memory — this analysis confirms it should
go first.

**2. Nickel — everything above ore is empty in global scope.**
Nodes: 7501/750110/750120 (mattes/oxide sinters/MHP — ID is 70% of the $44B
family exports and completely invisible at L1), 7502/750210/750220 (class-1
refined), 282540/283324 (nickel sulfate — the actual battery input). Indonesia's
midstream buildout is the single biggest structural story in the sector and our
concentration pillar cannot see it.
**Sources:** INSG monthly/annual country tables; IEA GCMO nickel refining;
company capacity roll-ups already partially in facilities (3 countries with
capacity on 7502 — the facility-derived shares ETL can floor-fill here).

**3. REE magnet chain — highest single-country concentration in scope, zero coverage.**
Nodes: 2846/284690 (compounds), 2805/280530 (metals), 850511/850519 (sintered
magnets — CN ≈ 90%). Magnet nodes also have NO trade rows at all (8505 family not
in Comtrade pulls), so post-gate even CN's event rows on those nodes are gated
out — the L0 layer goes fully dark there until either shares or trade land.
**Sources:** IEA GCMO rare-earths chapter (mine → oxide → metal → magnet by
country); Adamas Intelligence public summaries; USGS MCS REE processing text.
Ties to the NdFeB product-form decision (magnets are HS-node forms under REE).

**4. Graphite anode chain — both materials.**
Natural Graphite 380110/380120/380130/380190 (spherical/purified): zero shares
(ore is well covered, 18 countries). Synthetic Graphite 271312 (needle coke) +
380110: zero on both nodes — the material is entirely unscoreable for
concentration. CN ≈ 90%+ of spherical and ~70%+ of synthetic anode.
**Sources:** IEA GCMO graphite tables; USGS MCS graphite text (natural);
petroleum-coke trade literature for 271312. Benchmark queue item already exists —
raise its priority to match cobalt.

**5. Lithium chemicals.**
Nodes: 282520 (LiOH), 283691 (Li₂CO₃), 2805 (Li metal) — zero global shares
(ore: 9 countries, good). CN converts ~60–70% of chemicals; AU/CL feed it. The
2825/2836 family trade confirms CN #1 on hydroxide-family exports ($12.4B, 39%).
**Sources:** IEA GCMO lithium chemical production by country; USGS MCS lithium
(carbonate/hydroxide split appears in recent editions); AU government OCE
quarterly for conversion flows.

## Tier 2 — material pillar under-informed, moderate stage weight

**6. Manganese midstream.** 8111/811100 (refined Mn), 2820/282010 (dioxides),
284169 (sulfate — battery input, CN ≈ 90%+), 7202 ferro-Mn nodes. Ore is covered
(8 countries). **Sources:** IEA GCMO (battery-grade Mn sulfate table), Intl.
Manganese Institute annual review.

**7. Phosphate mid + battery.** 2809/280920 (phosphoric acid — MA 41% of $17B
family exports), 283525/283531/283539 (phosphates incl. LFP precursor). Ore is
the best-covered node in the DB (24 countries). **Sources:** IFA (International
Fertilizer Association) country production stats; USGS MCS phosphate rock text
for acid capacity.

**8. Copper blister 7402/740200.** ZM/CL dominate the $19B family. Lower urgency:
copper is fully covered at ore (14) and refined (17), so L1 already has strong
anchors on the 0.10 + 0.25 weights either side of it.

## Tier 3 — known, low urgency / accept as designed

* **Iron ore 260112/260120 concentrate** — empty because share propagation is
  same-stage-only (by design). 2601/260111 carry 17 countries; concentrate adds
  little discrimination. Accept.
* **Aluminum battery-grade foil 760711/760719** — foil is fabrication capacity,
  not resource concentration; alumina + smelter nodes are fully covered (19/12
  countries). Accept unless partner wants foil-specific coverage.
* **Copper foil 741011** — same reasoning as aluminum foil.
* **Non-launch materials** (Antimony, Chromium, Tungsten, Titanium, Magnesium,
  Sodium…) have large midstream holes too — out of scope for launch; Sodium
  deliberately unscored pending a soda-ash benchmark (see share-coverage memory).

## Secondary gap: Comtrade coverage itself

The participation gate is only as good as `trade_flows`. Two limits found while
sizing it: (a) ingestion is 4-digit aggregate only — fine for the gate now that
it matches at family level, but 6-digit detail would sharpen multi-material
baskets like 2833/2836; (b) several launch families were never pulled at all
(8505 magnets, 3801 graphite products, 2820, 283324…). A follow-up Comtrade run
over every distinct `left(hs_code_prefix,4)` in the mappings table would close
both the gate blind spots and improve the prioritization data here.

## Sequencing recommendation

1. **Cobalt benchmark pilot** (already queued) — validates the transcription →
   seed-workbook → loader path on the worst gap.
2. **Nickel + REE magnets + graphite (both) + lithium chemicals** — same
   mechanism, one workbook iteration each; these five close every Tier-1 hole.
3. **Facility-derived shares ETL** (fill-only, basis-tagged) as the floor for
   nodes benchmarks don't reach.
4. **Comtrade re-pull** over all launch families (secondary gap above).
5. Tier 2 rides with whichever benchmark source is already open (IEA GCMO covers
   Mn and phosphoric acid in the same document as cobalt/nickel/lithium).

## Data-hygiene footnote

2822 is staged `battery_grade` while its child 282200 is `intermediate` — same
code family, different stages. Harmless today (both share-less) but the stage
disagreement will matter the moment shares land on either. Worth a one-line fix
in the mappings seed before the cobalt pilot writes to these nodes.
