"""Curated seed data for company_material_exposures.

This is the single most important dataset for scoring accuracy. Without it the
scoring engine falls back to conservative defaults (0.5) for three of five pillars:

  - Material Concentration (30%): criticality = avg(exposure_score), default 0.5
  - Geopolitical/Trade (20%):     concentration = fraction of source_geography in
                                  HIGH_CONCENTRATION_GEOS {"CN","CD","RU"}, default 0.5
  - Operational (15%):            structural_dependency derived from SINGLE_SOURCE /
                                  CAPACITY_CONSTRAINT event subtypes — partially
                                  populated by events, but exposure records anchor it

Seeding approach
----------------
Each row represents one company's documented exposure to one material at one
supply chain stage. The UNIQUE constraint is (company_id, material_id,
supply_chain_stage), so the same company can appear multiple times for different
materials and/or at different stages (e.g. a vertically integrated miner+refiner).

exposure_score (0–1):
  Measures how much of this company's business/risk depends on this material.
  0.95 = primary commodity, near-total reliance
  0.70 = significant but not sole commodity
  0.40 = meaningful but secondary exposure

source_geography (ISO2):
  Where the material is physically extracted or the dominant sourcing origin
  for processed material. This is the key geopolitical signal — "where does
  the risk actually sit in the ground?"
  - For miners: their operating country
  - For refiners/CAM suppliers: where the raw ore or precursor comes from
    (often CN even for non-Chinese companies because China dominates midstream)
  - For cell makers: dominant upstream producing country for that material
  - For OEMs: the upstream anchor country in the full supply chain

supply_chain_stage values: mining | refining | cell | pack | oem

Run order:
    bdi-ingest seed-companies          # must run first
    bdi-ingest ingest-usgs ...         # materials table must be populated
    bdi-ingest seed-material-exposures # this script
"""

from __future__ import annotations

from datetime import date

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company, CompanyMaterialExposure
from app.models.supply import Material

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Seed data
# Each dict uses "company" (canonical_name) and "material" (canonical_name)
# which are resolved to UUIDs/IDs at runtime.
# ---------------------------------------------------------------------------

# The six canonical battery-critical materials in the DB:
# "Lithium", "Cobalt", "Nickel", "Manganese", "Natural Graphite", "Copper"

_AS_OF = date(2025, 1, 1)

_EXPOSURES: list[dict] = [

    # ── MINERS ──────────────────────────────────────────────────────────────

    # Albemarle: lithium miner, primary operations in Chile (Atacama) and Australia
    {
        "company": "Albemarle Corporation",
        "material": "Lithium",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "CL",
        "data_confidence": 0.90,
        "rationale": "World's largest lithium producer. Primary operations at Atacama brine (Chile) and Greenbushes spodumene JV (Australia). CL is dominant revenue source.",
    },

    # SQM: pure-play Atacama lithium
    {
        "company": "SQM",
        "material": "Lithium",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "CL",
        "data_confidence": 0.90,
        "rationale": "Atacama brine is SQM's primary revenue source. Second largest global lithium producer. Also produces potassium/iodine but lithium drives majority of earnings.",
    },

    # Glencore: cobalt and copper from DRC
    {
        "company": "Glencore",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.80,
        "source_geography": "CD",
        "data_confidence": 0.90,
        "rationale": "Largest cobalt producer globally via Mutanda Mining and KCC in DRC. Cobalt is a significant revenue stream though diversified across commodities.",
    },
    {
        "company": "Glencore",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.75,
        "source_geography": "CD",
        "data_confidence": 0.85,
        "rationale": "Major DRC copper producer (KCC, Katanga). Also operates in Chile and Kazakhstan. DRC is largest single-country source.",
    },

    # CMOC: cobalt and copper from DRC
    {
        "company": "CMOC Group",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.85,
        "source_geography": "CD",
        "data_confidence": 0.85,
        "rationale": "Second largest cobalt producer globally via TFM and Kisanfu in DRC. DRC operations represent majority of cobalt/copper output after 2022 expansion.",
    },
    {
        "company": "CMOC Group",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.80,
        "source_geography": "CD",
        "data_confidence": 0.85,
        "rationale": "TFM and Kisanfu are among the world's largest copper deposits. DRC is CMOC's dominant producing geography.",
    },

    # Huayou Cobalt: cobalt from DRC via subsidiaries
    {
        "company": "Huayou Cobalt",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.85,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "Significant DRC cobalt mining via CDM S.A. and other concessions. Huayou's core business is cobalt sourcing and processing — DRC is primary upstream origin.",
    },

    # Vale: nickel from Canada and Brazil
    {
        "company": "Vale",
        "material": "Nickel",
        "stage": "mining",
        "exposure_score": 0.85,
        "source_geography": "CA",
        "data_confidence": 0.90,
        "rationale": "World's largest nickel producer. Sudbury and Voisey's Bay (Canada) are primary operations. Vale Base Metals carved out separately but nickel remains core.",
    },
    {
        "company": "Vale",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.50,
        "source_geography": "BR",
        "data_confidence": 0.80,
        "rationale": "Copper is a byproduct at Vale's nickel operations and Carajás iron ore complex. Secondary to nickel but material.",
    },
    {
        "company": "Vale",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.45,
        "source_geography": "CA",
        "data_confidence": 0.75,
        "rationale": "Cobalt byproduct from nickel operations at Voisey's Bay and Sudbury. Not a primary commodity.",
    },

    # Norilsk Nickel: Russian operations — high geopolitical risk
    {
        "company": "Norilsk Nickel",
        "material": "Nickel",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "RU",
        "data_confidence": 0.85,
        "rationale": "World's largest nickel producer. Essentially all operations in Russia (Norilsk, Kola Peninsula). Russian entity under sanctions monitoring post-2022.",
    },
    {
        "company": "Norilsk Nickel",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.70,
        "source_geography": "RU",
        "data_confidence": 0.85,
        "rationale": "Significant copper byproduct from Russian operations. Third largest copper producer globally but Russian origin constrains Western buyer access.",
    },
    {
        "company": "Norilsk Nickel",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.50,
        "source_geography": "RU",
        "data_confidence": 0.80,
        "rationale": "Cobalt byproduct from nickel smelting. Material volumes but Russian sourcing exposure.",
    },

    # BHP: nickel (AU) and copper (CL)
    {
        "company": "BHP Group",
        "material": "Nickel",
        "stage": "mining",
        "exposure_score": 0.65,
        "source_geography": "AU",
        "data_confidence": 0.80,
        "rationale": "BHP Nickel West placed on care and maintenance May 2024 due to nickel price collapse. Reduced exposure score reflects operational suspension. Optionality remains.",
    },
    {
        "company": "BHP Group",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.85,
        "source_geography": "CL",
        "data_confidence": 0.90,
        "rationale": "Escondida (57.5% BHP, Chile) is world's largest copper mine by output. Copper is BHP's largest earnings contributor. CL is primary geographic exposure.",
    },

    # Freeport-McMoRan: copper-dominant
    {
        "company": "Freeport-McMoRan",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "ID",
        "data_confidence": 0.90,
        "rationale": "World's largest publicly traded copper producer. Grasberg (Indonesia) is primary asset. Also Cerro Verde (Peru) and Americas mines. Copper is ~85% of revenue.",
    },

    # Syrah Resources: graphite from Mozambique
    {
        "company": "Syrah Resources",
        "material": "Natural Graphite",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "MZ",
        "data_confidence": 0.85,
        "rationale": "Balama mine (Mozambique) is world's largest graphite deposit. Graphite is Syrah's sole commodity. Mozambique regional insurgency in Cabo Delgado is key operational risk.",
    },

    # Pilbara Minerals: lithium from Australia
    {
        "company": "Pilbara Minerals",
        "material": "Lithium",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "AU",
        "data_confidence": 0.90,
        "rationale": "Pilgangoora (WA) is one of world's largest hard rock lithium deposits. Lithium spodumene concentrate is Pilbara's sole product.",
    },

    # Rio Tinto: copper and lithium
    {
        "company": "Rio Tinto",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.75,
        "source_geography": "MN",
        "data_confidence": 0.85,
        "rationale": "Oyu Tolgoi (Mongolia) is world's fourth largest copper mine, ramping to full capacity. Also Kennecott (US). MN is the highest-risk sourcing geography.",
    },
    {
        "company": "Rio Tinto",
        "material": "Lithium",
        "stage": "mining",
        "exposure_score": 0.50,
        "source_geography": "AR",
        "data_confidence": 0.75,
        "rationale": "Acquired Arcadium Lithium (2025) giving Rincon project (Argentina) and other assets. Lithium is a growth category but not yet a core earnings driver.",
    },

    # Ivanhoe: copper from DRC
    {
        "company": "Ivanhoe Mines",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.90,
        "source_geography": "CD",
        "data_confidence": 0.85,
        "rationale": "Kamoa-Kakula (DRC, 39.6%) is world's second largest copper mine by resource. Copper is primary revenue. DRC concentration risk significant.",
    },

    # ── DRC MINING SUBSIDIARIES ──────────────────────────────────────────

    {
        "company": "Tenke Fungurume Mining",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.90,
        "source_geography": "CD",
        "data_confidence": 0.85,
        "rationale": "One of world's largest cobalt and copper mines. Located in Lualaba Province, DRC. CMOC 80% owned. Cobalt is a primary output.",
    },
    {
        "company": "Tenke Fungurume Mining",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.90,
        "source_geography": "CD",
        "data_confidence": 0.85,
        "rationale": "TFM is a major copper producer in DRC. Copper and cobalt are co-primary outputs. All production is in DRC.",
    },

    {
        "company": "Kisanfu Mining",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.90,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "High-grade cobalt/copper resource in DRC. CMOC 100%. Ramp-up phase as of 2024. Sole operation — full DRC concentration.",
    },
    {
        "company": "Kisanfu Mining",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.85,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "Copper co-product with cobalt at Kisanfu. Single-site, single-country operation in DRC.",
    },

    {
        "company": "Mutanda Mining",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "CD",
        "data_confidence": 0.85,
        "rationale": "World's single largest cobalt mine by historic output. Glencore 100%. Located in Lualaba, DRC. Sole asset — maximum single-site, single-country concentration.",
    },
    {
        "company": "Mutanda Mining",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.70,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "Copper byproduct from cobalt mining at Mutanda. Secondary commodity.",
    },

    {
        "company": "Kamoto Copper Company",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.90,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "Copper and cobalt mine in Kolwezi, DRC. Glencore 75%, Gécamines 25%. Significant DRC royalty dispute history. Sole operation in DRC.",
    },
    {
        "company": "Kamoto Copper Company",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.75,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "Cobalt co-product with copper at KCC. DRC-only operations.",
    },

    {
        "company": "Congo DPR Huayou Cobalt",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "Huayou-controlled DRC concessions (CDM S.A.). Cobalt is the sole commodity. Full DRC geographic concentration. Subject to ASM (artisanal mining) risk and human rights scrutiny.",
    },

    {
        "company": "PT Freeport Indonesia",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "ID",
        "data_confidence": 0.85,
        "rationale": "Operates Grasberg — world's largest gold/copper mine complex. Copper is primary output. Single-site, single-country operation in Papua, Indonesia. Freeport 48.76%, PT Inalum (state) 51.24%.",
    },

    {
        "company": "Cerro Verde",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "PE",
        "data_confidence": 0.85,
        "rationale": "Major copper producer in Arequipa, Peru. Freeport 53.56%. Single-site, single-country. Peru is generally stable but subject to regional community-relation risk.",
    },

    {
        "company": "Vale Base Metals",
        "material": "Nickel",
        "stage": "mining",
        "exposure_score": 0.85,
        "source_geography": "CA",
        "data_confidence": 0.85,
        "rationale": "Vale's nickel division. Sudbury and Voisey's Bay (Canada) are primary operations. Also PT Vale Indonesia. Canada is largest geographic source.",
    },
    {
        "company": "Vale Base Metals",
        "material": "Cobalt",
        "stage": "mining",
        "exposure_score": 0.55,
        "source_geography": "CA",
        "data_confidence": 0.80,
        "rationale": "Cobalt byproduct from nickel at Voisey's Bay. Material volumes but secondary to nickel.",
    },
    {
        "company": "Vale Base Metals",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.50,
        "source_geography": "CA",
        "data_confidence": 0.75,
        "rationale": "Copper byproduct from nickel operations at Sudbury. Secondary commodity.",
    },

    {
        "company": "BHP Nickel West",
        "material": "Nickel",
        "stage": "mining",
        "exposure_score": 0.90,
        "source_geography": "AU",
        "data_confidence": 0.75,
        "rationale": "Nickel mining and refining in Western Australia. On care and maintenance since May 2024. Nickel is sole commodity. Exposure score reflects retained asset base despite suspension.",
    },

    {
        "company": "Escondida",
        "material": "Copper",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "CL",
        "data_confidence": 0.90,
        "rationale": "World's largest copper mine by output. BHP 57.5%, Rio Tinto 30%. Atacama Desert, Chile. Copper is sole product. Maximum single-asset concentration.",
    },

    {
        "company": "Balama Graphite",
        "material": "Natural Graphite",
        "stage": "mining",
        "exposure_score": 0.95,
        "source_geography": "MZ",
        "data_confidence": 0.80,
        "rationale": "Syrah's Balama mine in Cabo Delgado, Mozambique. World's largest graphite deposit by resource. Graphite is sole commodity. Single-site, single-country — maximum concentration. Cabo Delgado insurgency is primary operational risk.",
    },

    # ── REFINERS / PROCESSORS ────────────────────────────────────────────────

    # Ganfeng: processes lithium from AU, CL, AR — refines in China
    {
        "company": "Ganfeng Lithium",
        "material": "Lithium",
        "stage": "refining",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.85,
        "rationale": "World's largest lithium compound producer. Refining operations predominantly in China, though raw spodumene sourced from Australia (Pilgangoora JV) and Argentina. Processing concentration in CN.",
    },

    # Tianqi: similar to Ganfeng
    {
        "company": "Tianqi Lithium",
        "material": "Lithium",
        "stage": "refining",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.85,
        "rationale": "Second largest lithium refiner. Refining primarily in China. Kwinana hydroxide plant (AU) adds some geographic diversification but CN remains dominant. Also holds 22.16% stake in SQM.",
    },

    # Umicore: cobalt and nickel refining in Belgium and China
    {
        "company": "Umicore",
        "material": "Cobalt",
        "stage": "refining",
        "exposure_score": 0.75,
        "source_geography": "CD",
        "data_confidence": 0.85,
        "rationale": "Leading CAM producer (NMC). Cobalt is a key input. Umicore sources cobalt precursors partly via China-processed DRC material. DRC is the upstream geographic anchor.",
    },
    {
        "company": "Umicore",
        "material": "Nickel",
        "stage": "refining",
        "exposure_score": 0.65,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Nickel sulphate for NMC CAM production. Precursors sourced via Chinese supply chain. Belgium refining but Chinese upstream concentration.",
    },

    # Sumitomo Metal Mining: nickel and cobalt, HPAL from Philippines
    {
        "company": "Sumitomo Metal Mining",
        "material": "Nickel",
        "stage": "refining",
        "exposure_score": 0.85,
        "source_geography": "PH",
        "data_confidence": 0.85,
        "rationale": "Major nickel refiner producing NCA cathode precursor. Nickel ore sourced from Philippines (Coral Bay HPAL JV, Taganito). Philippines is primary upstream geography.",
    },
    {
        "company": "Sumitomo Metal Mining",
        "material": "Cobalt",
        "stage": "refining",
        "exposure_score": 0.60,
        "source_geography": "PH",
        "data_confidence": 0.80,
        "rationale": "Cobalt byproduct from HPAL nickel refining in Philippines. Supplies Panasonic/Tesla NCA cathode chain.",
    },

    # Ecopro BM: Korean CAM, nickel and cobalt sourced via China
    {
        "company": "Ecopro BM",
        "material": "Nickel",
        "stage": "refining",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "South Korea's largest CAM producer (NCA/NMC). Nickel sulphate precursors sourced through Chinese supply chain. CN is the dominant upstream source despite KR refining location.",
    },
    {
        "company": "Ecopro BM",
        "material": "Cobalt",
        "stage": "refining",
        "exposure_score": 0.70,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Cobalt input for NMC/NCA CAM. Sourced via China-processed DRC material. CN intermediary concentration.",
    },
    {
        "company": "Ecopro BM",
        "material": "Lithium",
        "stage": "refining",
        "exposure_score": 0.65,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Lithium input for CAM processing. Sourced primarily through Chinese lithium chemical suppliers.",
    },

    # POSCO Future M: Korean CAM and anode
    {
        "company": "POSCO Future M",
        "material": "Nickel",
        "stage": "refining",
        "exposure_score": 0.70,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Produces NMC cathode. Nickel precursor supply chain routes through China. POSCO also has own nickel processing ambitions but current sourcing is CN-linked.",
    },
    {
        "company": "POSCO Future M",
        "material": "Lithium",
        "stage": "refining",
        "exposure_score": 0.65,
        "source_geography": "AU",
        "data_confidence": 0.80,
        "rationale": "POSCO Group has equity in Pilgangoora (Pilbara Minerals, AU) and operates lithium hydroxide processing in South Korea. AU is upstream geographic anchor.",
    },
    {
        "company": "POSCO Future M",
        "material": "Natural Graphite",
        "stage": "refining",
        "exposure_score": 0.70,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Produces graphite anode material. Natural graphite sourcing and processing dominated by CN supply chain.",
    },

    # BTR: graphite anode leader — China-centric
    {
        "company": "BTR New Energy",
        "material": "Natural Graphite",
        "stage": "refining",
        "exposure_score": 0.95,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "World's largest graphite anode material producer (~25% global share). Operations entirely in China. Both mining and processing concentrated in CN. Maximum single-material, single-country concentration.",
    },

    # ShanShan: graphite and some cathode materials
    {
        "company": "ShanShan Corporation",
        "material": "Natural Graphite",
        "stage": "refining",
        "exposure_score": 0.85,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Second largest graphite anode producer globally. Operations in China. Graphite is primary product.",
    },
    {
        "company": "ShanShan Corporation",
        "material": "Cobalt",
        "stage": "refining",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Also produces cathode materials including NMC. Cobalt input sourced via Chinese supply chain from DRC.",
    },

    # BASF: European CAM with DRC cobalt upstream
    {
        "company": "BASF",
        "material": "Cobalt",
        "stage": "refining",
        "exposure_score": 0.65,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "European CAM (NMC) producer via BASF Catalysts. Cobalt sourced from DRC through trading channels. Also has JV with Nornickel for recycling. CD is upstream anchor.",
    },
    {
        "company": "BASF",
        "material": "Nickel",
        "stage": "refining",
        "exposure_score": 0.55,
        "source_geography": "RU",
        "data_confidence": 0.75,
        "rationale": "Nickel input for NMC. Historically sourced from Norilsk Nickel (Russia). Battery JV with Nornickel. Post-2022 supply chain diversification underway but RU exposure remains.",
    },

    # ── CELL MAKERS ──────────────────────────────────────────────────────────

    # CATL: produces LFP (Li+Graphite+Mn) and NMC (Li+Co+Ni+Mn+Graphite)
    {
        "company": "CATL",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.85,
        "rationale": "Largest battery cell maker globally. Lithium input sourced from Chile/Australia but processed in China by Ganfeng, Tianqi and others. CN refining concentration is the dominant risk.",
    },
    {
        "company": "CATL",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.85,
        "rationale": "China controls ~70% of natural graphite processing. CATL sources anode materials domestically from BTR, ShanShan. Maximum CN concentration.",
    },
    {
        "company": "CATL",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.70,
        "source_geography": "CD",
        "data_confidence": 0.85,
        "rationale": "NMC cells require cobalt. DRC is the dominant upstream source. CATL is shifting toward LFP (cobalt-free) but still major NMC producer.",
    },
    {
        "company": "CATL",
        "material": "Nickel",
        "stage": "cell",
        "exposure_score": 0.75,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "High-nickel NMC cells for energy density. Nickel sulphate sourced from Chinese-processed Indonesian/Philippine ore.",
    },
    {
        "company": "CATL",
        "material": "Manganese",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "LFP and NMC both use manganese. China dominates manganese production. Domestic sourcing for CATL.",
    },

    # BYD: LFP-dominant (Li+Mn+Graphite), vertically integrated
    {
        "company": "BYD",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.85,
        "rationale": "Vertically integrated — owns lithium assets in Chile and Australia, processes in China. LFP-dominant chemistry. High lithium intensity.",
    },
    {
        "company": "BYD",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.85,
        "rationale": "Anode material sourced domestically in China. Full CN concentration.",
    },
    {
        "company": "BYD",
        "material": "Manganese",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "LFP uses manganese. BYD sources domestically in China.",
    },
    {
        "company": "BYD",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.35,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "BYD is heavily LFP-oriented (cobalt-free). Minimal cobalt exposure through residual NMC product mix.",
    },

    # LG Energy Solution: NMC-dominant, Korean company with CN supply chain
    {
        "company": "LG Energy Solution",
        "material": "Nickel",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "High-nickel NMC (NCMA) is LGES's flagship chemistry. Nickel sulphate precursors flow through Chinese supply chain before reaching Korean/US gigafactories.",
    },
    {
        "company": "LG Energy Solution",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.75,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "NMC cells require cobalt. DRC is upstream anchor. LGES is reducing cobalt content but still significant exposure.",
    },
    {
        "company": "LG Energy Solution",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Lithium hydroxide sourced primarily from Chinese refiners (Ganfeng, Tianqi) and some Australian origin. CN refining concentration dominates.",
    },
    {
        "company": "LG Energy Solution",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.85,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Anode graphite predominantly from Chinese suppliers (BTR, ShanShan). CN concentration for anode materials is near-total.",
    },
    {
        "company": "LG Energy Solution",
        "material": "Manganese",
        "stage": "cell",
        "exposure_score": 0.65,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Manganese sulphate for NMC cathode. Chinese supply chain.",
    },

    # Samsung SDI: NMC-dominant, similar to LGES
    {
        "company": "Samsung SDI",
        "material": "Nickel",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "High-nickel NMC (Gen5) is primary chemistry. Nickel precursor supply chain routes through China.",
    },
    {
        "company": "Samsung SDI",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.70,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "NMC cells require cobalt. DRC is upstream source. SDI supplying Rivian and BMW — both have cobalt traceability requirements.",
    },
    {
        "company": "Samsung SDI",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Lithium hydroxide sourced from Chinese refiners predominantly.",
    },
    {
        "company": "Samsung SDI",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.85,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Anode graphite from Chinese suppliers. Near-total CN concentration for this input.",
    },

    # SK On: NMC, financial difficulties noted
    {
        "company": "SK On",
        "material": "Nickel",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "High-nickel NMC. Nickel supply chain routes through Chinese processors. SK On is expanding but sourcing diversification is limited.",
    },
    {
        "company": "SK On",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.70,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "NMC cobalt input from DRC via Chinese processing. SK On has BlueOval SK (Ford) and SKBA (VW) JVs in US and Germany.",
    },
    {
        "company": "SK On",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.75,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Lithium hydroxide from Chinese refiners predominantly. Some diversification via Australian sources underway.",
    },
    {
        "company": "SK On",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.85,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Anode graphite from Chinese suppliers. Maximum CN concentration.",
    },

    # Panasonic Energy: NCA (Ni-Co, no Mn) cylinders for Tesla
    {
        "company": "Panasonic Energy",
        "material": "Nickel",
        "stage": "cell",
        "exposure_score": 0.85,
        "source_geography": "PH",
        "data_confidence": 0.80,
        "rationale": "NCA chemistry is nickel-dominant (~80%). Sumitomo Metal Mining supplies NCA precursor with Philippine nickel. PH is upstream geographic anchor.",
    },
    {
        "company": "Panasonic Energy",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.65,
        "source_geography": "PH",
        "data_confidence": 0.80,
        "rationale": "NCA contains cobalt (~5%). Sourced via SMM Philippines HPAL chain.",
    },
    {
        "company": "Panasonic Energy",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "AU",
        "data_confidence": 0.80,
        "rationale": "Panasonic sources lithium hydroxide partially from Australian spodumene route (Pilbara-linked). AU is primary upstream geography.",
    },
    {
        "company": "Panasonic Energy",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.85,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Anode graphite from Chinese processors despite Panasonic's efforts to diversify. CN concentration for this input.",
    },

    # CALB: Chinese LFP specialist
    {
        "company": "CALB Group",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "LFP-dominant Chinese cell maker. Lithium sourced domestically. Full CN concentration.",
    },
    {
        "company": "CALB Group",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Anode graphite from Chinese domestic suppliers. Full CN concentration.",
    },
    {
        "company": "CALB Group",
        "material": "Manganese",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "LFP uses manganese phosphate. Chinese supply chain.",
    },

    # Gotion: Chinese LFP
    {
        "company": "Gotion High-tech",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "LFP-dominant cell maker. Full Chinese supply chain. VW holds ~26% but sourcing remains CN.",
    },
    {
        "company": "Gotion High-tech",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Domestic Chinese graphite supply chain.",
    },
    {
        "company": "Gotion High-tech",
        "material": "Manganese",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "LFP manganese input from domestic CN sources.",
    },

    # Northvolt: NMC, European manufacturer with mixed supply chain
    {
        "company": "Northvolt",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.65,
        "rationale": "NMC cell producer. Lithium supply partially from Chinese refiners. Post-bankruptcy restructuring may alter supply chain. Data confidence reduced given 2024 bankruptcy filing.",
    },
    {
        "company": "Northvolt",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.70,
        "source_geography": "CD",
        "data_confidence": 0.65,
        "rationale": "NMC cobalt from DRC upstream. Northvolt committed to responsible sourcing via BASF partnership.",
    },
    {
        "company": "Northvolt",
        "material": "Nickel",
        "stage": "cell",
        "exposure_score": 0.75,
        "source_geography": "NO",
        "data_confidence": 0.65,
        "rationale": "Northvolt has supply agreements with Nordic producers and BASF. Norway/Finland nickel represents a partial diversification from CN/RU. Data confidence reduced.",
    },
    {
        "company": "Northvolt",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.65,
        "rationale": "Anode graphite predominantly CN-sourced. Limited non-Chinese anode supply exists.",
    },

    # Cell maker subsidiaries

    {
        "company": "FinDreams Battery",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "BYD's Blade Battery subsidiary. LFP chemistry — lithium-intensive. Full CN supply chain via BYD's vertically integrated operations.",
    },
    {
        "company": "FinDreams Battery",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.90,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Anode materials sourced domestically via BYD group supply chain.",
    },
    {
        "company": "FinDreams Battery",
        "material": "Manganese",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "LFP manganese from Chinese domestic supply.",
    },

    {
        "company": "PowerCo SE",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.75,
        "source_geography": "CN",
        "data_confidence": 0.65,
        "rationale": "VW's battery subsidiary, still ramping. Initially reliant on Chinese-processed lithium. Transitioning as Salzgitter and other gigafactories come online.",
    },
    {
        "company": "PowerCo SE",
        "material": "Nickel",
        "stage": "cell",
        "exposure_score": 0.70,
        "source_geography": "CN",
        "data_confidence": 0.65,
        "rationale": "NMC/prismatic cell format. Nickel precursor from CN supply chain during ramp phase.",
    },
    {
        "company": "PowerCo SE",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.65,
        "source_geography": "CD",
        "data_confidence": 0.65,
        "rationale": "NMC cobalt input. VW is pursuing responsible cobalt sourcing initiatives but DRC remains upstream anchor.",
    },
    {
        "company": "PowerCo SE",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.65,
        "rationale": "Anode graphite supply CN-dominated. Limited non-Chinese anode alternatives at scale.",
    },

    {
        "company": "Envision AESC",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "NMC cell maker (formerly Nissan's AESC). Now Chinese-owned (Envision Group). Supply chain CN-linked.",
    },
    {
        "company": "Envision AESC",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.70,
        "source_geography": "CD",
        "data_confidence": 0.70,
        "rationale": "NMC cobalt from DRC via CN processing channels.",
    },
    {
        "company": "Envision AESC",
        "material": "Nickel",
        "stage": "cell",
        "exposure_score": 0.75,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "NMC nickel precursor from Chinese supply chain.",
    },
    {
        "company": "Envision AESC",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.85,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Anode graphite from Chinese suppliers.",
    },

    {
        "company": "Prime Planet and Energy Solutions",
        "material": "Lithium",
        "stage": "cell",
        "exposure_score": 0.75,
        "source_geography": "AU",
        "data_confidence": 0.75,
        "rationale": "Toyota/Panasonic JV. Panasonic's influence drives Australian lithium sourcing (Pilbara-linked). AU is primary upstream geography.",
    },
    {
        "company": "Prime Planet and Energy Solutions",
        "material": "Nickel",
        "stage": "cell",
        "exposure_score": 0.70,
        "source_geography": "PH",
        "data_confidence": 0.75,
        "rationale": "NMC cells. Nickel via Sumitomo Metal Mining HPAL chain (Philippines).",
    },
    {
        "company": "Prime Planet and Energy Solutions",
        "material": "Cobalt",
        "stage": "cell",
        "exposure_score": 0.60,
        "source_geography": "PH",
        "data_confidence": 0.70,
        "rationale": "Cobalt byproduct from SMM HPAL chain. Philippines source.",
    },
    {
        "company": "Prime Planet and Energy Solutions",
        "material": "Natural Graphite",
        "stage": "cell",
        "exposure_score": 0.80,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Anode graphite from Chinese suppliers despite Japanese/Toyota group's diversification efforts.",
    },

    # ── OEMs ─────────────────────────────────────────────────────────────────
    # OEM exposures are indirect. exposure_score is lower to reflect that risk
    # is mediated through cell maker supply chains. source_geography reflects
    # the dominant upstream origin for each material reaching the OEM's batteries.

    {
        "company": "Tesla",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CL",
        "data_confidence": 0.75,
        "rationale": "Tesla has direct lithium offtake agreements with Chilean and Australian miners. More supply chain visibility than typical OEMs. CL is primary source.",
    },
    {
        "company": "Tesla",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.40,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "Tesla has reduced cobalt intensity significantly (4680 low-cobalt NMC, 30% of fleet on LFP). DRC is upstream anchor for remaining cobalt exposure.",
    },
    {
        "company": "Tesla",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "High-nickel cells from Panasonic/LGES/CATL. Nickel supply chain CN-linked for the Chinese-produced portion.",
    },
    {
        "company": "Tesla",
        "material": "Natural Graphite",
        "stage": "oem",
        "exposure_score": 0.65,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Anode graphite in Tesla cells overwhelmingly CN-sourced. Tesla has Syrah Vidalia offtake (MZ/US) but volume is small vs. CN-sourced portion.",
    },

    {
        "company": "Volkswagen Group",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "VW sources cells from CATL, SK On. Lithium in those cells is predominantly CN-refined. PowerCo SE in-house cells will change this over time.",
    },
    {
        "company": "Volkswagen Group",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "NMC cells from CATL/SK On contain cobalt. DRC is upstream source. VW has cobalt responsible sourcing program.",
    },
    {
        "company": "Volkswagen Group",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "High-nickel NMC from SK On and CATL. CN supply chain for nickel precursors.",
    },
    {
        "company": "Volkswagen Group",
        "material": "Natural Graphite",
        "stage": "oem",
        "exposure_score": 0.60,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Anode graphite in VW cells predominantly CN-sourced.",
    },

    {
        "company": "BMW Group",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.80,
        "rationale": "BMW buys cells from Samsung SDI, CATL, Northvolt. All NMC — cobalt from DRC upstream. BMW has one of the most advanced cobalt traceability programs (direct smelter agreements).",
    },
    {
        "company": "BMW Group",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "High-nickel NMC from SDI and CATL. Nickel supply chain CN-linked.",
    },
    {
        "company": "BMW Group",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Lithium in BMW cells from CN-processed sources predominately.",
    },
    {
        "company": "BMW Group",
        "material": "Natural Graphite",
        "stage": "oem",
        "exposure_score": 0.60,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Anode graphite in BMW cells predominantly CN-sourced.",
    },

    {
        "company": "General Motors",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Ultium cells from LGES JV. Lithium primarily CN-refined. GM has direct offtake with Livent and others for IRA compliance.",
    },
    {
        "company": "General Motors",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "Ultium NMC chemistry contains cobalt. DRC upstream source via LGES supply chain.",
    },
    {
        "company": "General Motors",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "High-nickel Ultium cells. CN-linked nickel supply chain.",
    },
    {
        "company": "General Motors",
        "material": "Natural Graphite",
        "stage": "oem",
        "exposure_score": 0.60,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Anode graphite in Ultium cells predominantly CN-sourced.",
    },

    {
        "company": "Ford Motor Company",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "BlueOval SK cells (SK On JV). Lithium CN-refined predominantly. Ford also has CATL LFP cells for some models.",
    },
    {
        "company": "Ford Motor Company",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "NMC cells via SK On. DRC cobalt upstream.",
    },
    {
        "company": "Ford Motor Company",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "High-nickel NMC from SK On. CN nickel supply chain.",
    },
    {
        "company": "Ford Motor Company",
        "material": "Natural Graphite",
        "stage": "oem",
        "exposure_score": 0.60,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Anode graphite from CN-dominant supply chain.",
    },

    {
        "company": "Hyundai Motor Company",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "HES JV (LG Energy Solution) and SK On supply. Lithium from CN-refined sources predominantly.",
    },
    {
        "company": "Hyundai Motor Company",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "NMC cells — DRC cobalt upstream.",
    },
    {
        "company": "Hyundai Motor Company",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "High-nickel NMC (Ioniq platform). CN nickel supply chain.",
    },
    {
        "company": "Hyundai Motor Company",
        "material": "Natural Graphite",
        "stage": "oem",
        "exposure_score": 0.60,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Anode graphite predominantly CN-sourced.",
    },

    {
        "company": "Stellantis",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "StarPlus Energy JV (Samsung SDI) — NMC cells. DRC cobalt upstream.",
    },
    {
        "company": "Stellantis",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "NMC cells from SDI and ACC JV. CN nickel precursor supply chain.",
    },
    {
        "company": "Stellantis",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Lithium from CN-refined sources in SDI supply chain.",
    },

    {
        "company": "Mercedes-Benz Group",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "CATL and ACC supply NMC cells. DRC cobalt in supply chain.",
    },
    {
        "company": "Mercedes-Benz Group",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "High-nickel NMC from CATL. CN nickel precursor chain.",
    },
    {
        "company": "Mercedes-Benz Group",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Lithium from CN-refined sources via CATL supply chain.",
    },

    # New OEMs
    {
        "company": "Toyota Motor Corporation",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.45,
        "source_geography": "AU",
        "data_confidence": 0.75,
        "rationale": "Toyota/PPES JV uses Panasonic-influenced supply chain (Australian lithium). Battery EV volumes still lower than peers, hence lower exposure score.",
    },
    {
        "company": "Toyota Motor Corporation",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "PH",
        "data_confidence": 0.75,
        "rationale": "NMC/HEV NiMH demand. Nickel via SMM HPAL chain (Philippines) for PPES supply.",
    },
    {
        "company": "Toyota Motor Corporation",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.40,
        "source_geography": "PH",
        "data_confidence": 0.70,
        "rationale": "Cobalt input via PPES/SMM chain. Toyota is shifting toward solid-state and LFP to reduce cobalt dependency.",
    },
    {
        "company": "Toyota Motor Corporation",
        "material": "Natural Graphite",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Anode graphite CN-sourced. Toyota's solid-state push will eventually reduce graphite dependence.",
    },

    {
        "company": "Honda Motor Company",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.45,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "LG Energy Solution JV for US. Lithium CN-refined predominantly. Honda ramping EV volume from low base.",
    },
    {
        "company": "Honda Motor Company",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.45,
        "source_geography": "CD",
        "data_confidence": 0.70,
        "rationale": "NMC cells via LGES JV. DRC cobalt upstream.",
    },
    {
        "company": "Honda Motor Company",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "NMC nickel from CN supply chain via LGES.",
    },

    {
        "company": "Nissan Motor Company",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.45,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Envision AESC as primary cell supplier. CN-linked supply chain.",
    },
    {
        "company": "Nissan Motor Company",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.45,
        "source_geography": "CD",
        "data_confidence": 0.70,
        "rationale": "NMC cells from Envision AESC. DRC cobalt upstream.",
    },
    {
        "company": "Nissan Motor Company",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "NMC nickel via Envision AESC supply chain.",
    },

    {
        "company": "Subaru Corporation",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.40,
        "source_geography": "AU",
        "data_confidence": 0.70,
        "rationale": "Solterra co-developed with Toyota — shares Toyota/PPES supply chain. Low EV volumes. Australian lithium via PPES/Panasonic.",
    },
    {
        "company": "Subaru Corporation",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.40,
        "source_geography": "PH",
        "data_confidence": 0.65,
        "rationale": "Via PPES/Toyota supply chain. Philippines nickel source.",
    },

    {
        "company": "Rivian Automotive",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Samsung SDI (NMC) is primary cell supplier. CN nickel precursor chain. Single cell supplier = elevated concentration risk.",
    },
    {
        "company": "Rivian Automotive",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CD",
        "data_confidence": 0.70,
        "rationale": "NMC from Samsung SDI. DRC cobalt upstream. Rivian has traceability requirements in Amazon van contract.",
    },
    {
        "company": "Rivian Automotive",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "Lithium via Samsung SDI supply chain. CN-refined source.",
    },
    {
        "company": "Rivian Automotive",
        "material": "Natural Graphite",
        "stage": "oem",
        "exposure_score": 0.60,
        "source_geography": "CN",
        "data_confidence": 0.65,
        "rationale": "Anode graphite CN-sourced via SDI chain. Rivian is a single-factory, single-supplier company — maximum concentration risk.",
    },

    {
        "company": "Lucid Group",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.45,
        "source_geography": "CN",
        "data_confidence": 0.65,
        "rationale": "Lucid sources cells externally (supplier not publicly disclosed). CN-linked lithium supply chain assumed given market structure.",
    },
    {
        "company": "Lucid Group",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.60,
        "rationale": "High-efficiency drivetrain suggests high-nickel chemistry. CN supply chain assumed.",
    },

    {
        "company": "Geely Auto Group",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.65,
        "source_geography": "CN",
        "data_confidence": 0.80,
        "rationale": "Chinese OEM with CATL and CALB as primary cell suppliers. Full CN supply chain. LFP-dominant domestic models.",
    },
    {
        "company": "Geely Auto Group",
        "material": "Natural Graphite",
        "stage": "oem",
        "exposure_score": 0.65,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Anode graphite entirely CN-sourced via CATL/CALB supply chain.",
    },
    {
        "company": "Geely Auto Group",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.45,
        "source_geography": "CD",
        "data_confidence": 0.70,
        "rationale": "NMC models use cobalt. DRC upstream. LFP models reduce overall cobalt exposure.",
    },

    {
        "company": "Volvo Car Group",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "CATL is Volvo's primary cell supplier. CN lithium supply chain.",
    },
    {
        "company": "Volvo Car Group",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "NMC cells from CATL. DRC cobalt upstream.",
    },
    {
        "company": "Volvo Car Group",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "High-nickel NMC from CATL. CN supply chain.",
    },

    {
        "company": "Polestar Automotive",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "CATL as primary cell supplier. Manufacturing in China. Full CN supply chain concentration.",
    },
    {
        "company": "Polestar Automotive",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CD",
        "data_confidence": 0.70,
        "rationale": "NMC cells from CATL. DRC cobalt upstream.",
    },
    {
        "company": "Polestar Automotive",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "High-nickel CATL NMC. Full CN nickel supply chain.",
    },

    {
        "company": "Zeekr",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.65,
        "source_geography": "CN",
        "data_confidence": 0.70,
        "rationale": "CATL cells for Zeekr models. Chinese OEM — full CN supply chain.",
    },
    {
        "company": "Zeekr",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.65,
        "rationale": "NMC cells from CATL. DRC cobalt upstream.",
    },

    {
        "company": "Kia Corporation",
        "material": "Nickel",
        "stage": "oem",
        "exposure_score": 0.55,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "SK On, LGES, Samsung SDI supply Kia. High-nickel NMC. CN nickel supply chain.",
    },
    {
        "company": "Kia Corporation",
        "material": "Cobalt",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CD",
        "data_confidence": 0.75,
        "rationale": "NMC cells from Korean suppliers. DRC cobalt upstream.",
    },
    {
        "company": "Kia Corporation",
        "material": "Lithium",
        "stage": "oem",
        "exposure_score": 0.50,
        "source_geography": "CN",
        "data_confidence": 0.75,
        "rationale": "Lithium via Korean cell makers — CN-refined sources predominately.",
    },

    # ── RECYCLERS ───────────────────────────────────────────────────────────
    # Recyclers have material exposure but from secondary/recovered feedstock.
    # source_geography = US (where they collect and process)
    # exposure_score is moderate — they depend on material prices but have
    # insulated supply (collected domestically) rather than geopolitical upstream.

    {
        "company": "Redwood Materials",
        "material": "Lithium",
        "stage": "refining",
        "exposure_score": 0.70,
        "source_geography": "US",
        "data_confidence": 0.70,
        "rationale": "Recycles and remanufactures lithium from end-of-life batteries. US-based feedstock collection. Produces recycled cathode — lithium is primary commodity.",
    },
    {
        "company": "Redwood Materials",
        "material": "Cobalt",
        "stage": "refining",
        "exposure_score": 0.65,
        "source_geography": "US",
        "data_confidence": 0.70,
        "rationale": "Cobalt recovered from NMC/NCA scrap. US-based processing. Secondary supply — less geopolitical exposure than primary miners.",
    },
    {
        "company": "Redwood Materials",
        "material": "Nickel",
        "stage": "refining",
        "exposure_score": 0.60,
        "source_geography": "US",
        "data_confidence": 0.65,
        "rationale": "Nickel recovered from NMC/NCA battery black mass. US-sourced feedstock.",
    },
    {
        "company": "Redwood Materials",
        "material": "Copper",
        "stage": "refining",
        "exposure_score": 0.55,
        "source_geography": "US",
        "data_confidence": 0.65,
        "rationale": "Copper foil produced from recycled anode scrap. US-based.",
    },

    {
        "company": "Cirba Solutions",
        "material": "Lithium",
        "stage": "refining",
        "exposure_score": 0.65,
        "source_geography": "US",
        "data_confidence": 0.65,
        "rationale": "Largest US battery recycler by volume. Recovers lithium from black mass. Facilities in Ohio and South Carolina.",
    },
    {
        "company": "Cirba Solutions",
        "material": "Cobalt",
        "stage": "refining",
        "exposure_score": 0.60,
        "source_geography": "US",
        "data_confidence": 0.65,
        "rationale": "Cobalt recovered from processed black mass. US-domestic feedstock.",
    },
    {
        "company": "Cirba Solutions",
        "material": "Nickel",
        "stage": "refining",
        "exposure_score": 0.55,
        "source_geography": "US",
        "data_confidence": 0.60,
        "rationale": "Nickel recovered from NMC scrap at US facilities.",
    },
]


# ---------------------------------------------------------------------------
# Seed function
# ---------------------------------------------------------------------------

def seed_material_exposures(session: Session) -> dict[str, int]:
    """Upsert company_material_exposures from curated seed data.

    Resolves company canonical_name → company_id and material canonical_name →
    material_id at runtime. Skips rows where the company or material is not
    found in the DB (logs a warning).

    Upsert behaviour:
      - New rows are inserted.
      - Existing rows (matched on company_id + material_id + supply_chain_stage)
        update exposure_score, source_geography, data_confidence, rationale,
        and as_of_date.
      - Structural identity fields (company, material, stage) are immutable.

    Returns {"inserted": int, "updated": int, "skipped": int}
    """
    # Pre-load company and material lookups to avoid N+1 queries.
    companies: dict[str, "uuid.UUID"] = {
        row.canonical_name: row.id
        for row in session.scalars(select(Company))
    }
    materials: dict[str, int] = {
        row.canonical_name: row.id
        for row in session.scalars(select(Material))
    }

    inserted = 0
    updated = 0
    skipped = 0

    for entry in _EXPOSURES:
        company_name = entry["company"]
        material_name = entry["material"]

        company_id = companies.get(company_name)
        material_id = materials.get(material_name)

        if company_id is None:
            log.warning(
                "seed_material_exposures.company_not_found",
                company=company_name,
                material=material_name,
            )
            skipped += 1
            continue

        if material_id is None:
            log.warning(
                "seed_material_exposures.material_not_found",
                company=company_name,
                material=material_name,
            )
            skipped += 1
            continue

        stage = entry["stage"]
        existing = session.scalar(
            select(CompanyMaterialExposure).where(
                CompanyMaterialExposure.company_id == company_id,
                CompanyMaterialExposure.material_id == material_id,
                CompanyMaterialExposure.supply_chain_stage == stage,
            )
        )

        if existing is None:
            session.add(
                CompanyMaterialExposure(
                    company_id=company_id,
                    material_id=material_id,
                    supply_chain_stage=stage,
                    exposure_score=entry["exposure_score"],
                    source_geography=entry.get("source_geography"),
                    data_confidence=entry.get("data_confidence"),
                    rationale=entry.get("rationale"),
                    as_of_date=_AS_OF,
                    score_derivation="curated_seed",  # migration 053 provenance
                )
            )
            log.info(
                "seed_material_exposures.inserted",
                company=company_name,
                material=material_name,
                stage=stage,
            )
            inserted += 1
        else:
            # Partial upsert — update scores and rationale, preserve identity.
            changed = False
            mutable = {
                "exposure_score": entry["exposure_score"],
                "source_geography": entry.get("source_geography"),
                "data_confidence": entry.get("data_confidence"),
                "rationale": entry.get("rationale"),
                "as_of_date": _AS_OF,
                "score_derivation": "curated_seed",  # migration 053 provenance
            }
            if existing.rationale and "[workbook]" in (existing.rationale or ""):
                # The workbook loader appended filing evidence to this row's
                # rationale (seed_company_workbook.py) — don't clobber it,
                # and keep the fresher workbook as_of_date.
                mutable.pop("rationale")
                mutable.pop("as_of_date")
            for field, value in mutable.items():
                if getattr(existing, field) != value:
                    setattr(existing, field, value)
                    changed = True

            if changed:
                log.info(
                    "seed_material_exposures.updated",
                    company=company_name,
                    material=material_name,
                    stage=stage,
                )
            else:
                log.debug(
                    "seed_material_exposures.unchanged",
                    company=company_name,
                    material=material_name,
                    stage=stage,
                )
            updated += 1

    session.commit()

    log.info(
        "seed_material_exposures.done",
        inserted=inserted,
        updated=updated,
        skipped=skipped,
    )
    return {"inserted": inserted, "updated": updated, "skipped": skipped}
