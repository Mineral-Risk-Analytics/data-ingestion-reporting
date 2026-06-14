"""Single source of truth for the regulations schema group.

Populates four tables in dependency order:
  1. regulations              — the regulatory instrument records
  2. regulation_material_scope — which materials each regulation covers
  3. regulation_geography_scope — which geographies each regulation applies to
  4. company_regulation_exposure — each company's exposure and compliance status

This file is the single authoritative place for regulation reference data.
Ingesters (eurlex.py, federal_register, etc.) MUST NOT define their own
regulations — they look up regulation_id via ``regulation_aliases`` and
update only mutable fields (summary, latest URL, etc.).  See
``seed_regulation_aliases.py`` for the external-ID → regulation_key map.

Why this matters for scoring
-----------------------------
The regulatory pillar (20% of company risk score) reads these tables.  When
``company_regulation_exposure`` is populated, ``get_active_compliance_obligations()``
in ``evidence_query.py`` returns structured obligation keys which map to
hard-coded uplift points in ``regulatory_risk.py``:

    UFLPA              → 25 pts   (Xinjiang forced labor supply chain exposure)
    EU_BATTERY_REG_2023 → 20 pts   (EU market participants lacking compliance docs)
    IRA_DOMESTIC       → 15 pts   (FEOC materials disqualifying US tax credits)

Other seeded regulations (CRMA_2024, EU_CBAM, EU_CSDDD, EU_CONFLICT_MINERALS,
EU_REACH_COBALT, SEC_CLIMATE_2024) are tracked for evidence and reporting but
generate no uplift until ``COMPLIANCE_OBLIGATIONS`` is extended.

Trust model — verified flag
---------------------------
Every row this file inserts is marked ``verified=True``.  Scoring code
filters on this flag so events attributed to auto-created or partner-
unreviewed regulations don't influence scores.  Only this seed (and
``seed_regulation_aliases``) is allowed to write ``verified=True`` rows.

compliance_status logic
-----------------------
Only non_compliant | partial | unknown generate uplift. "compliant" rows are
inserted but produce no score effect — they document confirmed compliance.

    non_compliant  Company is actively violating or materially failing the regulation
    partial        In scope, working toward compliance, not yet achieved
    unknown        In scope but compliance status not publicly verifiable
    compliant      Confirmed compliance — no uplift, no active regulatory burden

Run order:
    bdi-ingest seed-companies          # companies must exist
    bdi-ingest seed-materials          # materials must exist
    bdi-ingest seed-regulations        # this script
    bdi-ingest seed-regulation-aliases # CELEX / FR doc → regulation_key
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.regulatory import (
    CompanyRegulationExposure,
    Regulation,
    RegulationGeographyScope,
    RegulationMaterialScope,
)
from app.models.supply import Material

log = structlog.get_logger(__name__)

_ASSESSED_AT = date(2025, 1, 1)

# ---------------------------------------------------------------------------
# Seed version
# ---------------------------------------------------------------------------
# Bumped whenever the curated regulation roster, material scopes, or geography
# scopes change in a way partner-side reviewers should re-validate.  Stamped
# into ``regulations.metadata_json.seed_version`` for every seeded row so the
# DB carries provenance.  Format: YYYY-MM-DD of the change.
#
# 2026-05-17 — partner-direction scope_type re-curation:
#   UFLPA materials              : covered → banned
#   EU_BATTERY_REG_2023 materials: disclosure_required → restricted
#   EU_CONFLICT_MINERALS         : Cobalt removed (mis-scoped),
#                                   Tin/Tantalum/Tungsten added at
#                                   disclosure_required (3TG actual scope)
#   EU_CBAM                      : Iron Ore (LFP Grade) added at covered
#
# 2026-05-17 (later same session) — Section 3 material-scope expansions:
#   IRA_DOMESTIC                 : metadata_json severity_overrides
#                                   added (effective: 0.85) reflecting
#                                   FEOC critical-mineral rule bite
#   CRMA_2024                    : Nd/Pr/Dy/Tb promoted strategic →
#                                   restricted (already in 65%-cap
#                                   violation today, bite is real)
#                                : Phosphate (Battery Grade) +
#                                   Rare Earth Elements (bundled)
#                                   added at strategic_raw_material
#                                   (closes phosphate + bundled-REE
#                                   coverage gaps)
#   EU_BATTERY_REG_2023          : Phosphate (Battery Grade) +
#                                   Iron Ore (LFP Grade) added at
#                                   restricted; Aluminum added at
#                                   covered (cell-component coverage,
#                                   no recycled-content threshold)
#
# 2026-05-17 (Section 4 walkthrough — Option C geography curation):
#   EU_CBAM                      : removed CN + RU from targeted_country
#                                   (universal trade-flow, not sanctions)
#   CRMA_2024                    : added ID, CL, CD, RU to
#                                   targeted_country alongside CN
#                                   (reflects single-non-EU-country
#                                   supply concentration risk for
#                                   strategic materials)
_SEED_VERSION = "2026-06-11"

# ---------------------------------------------------------------------------
# 1. Regulation definitions
# ---------------------------------------------------------------------------
# regulation_key MUST match the keys in regulatory_risk.COMPLIANCE_OBLIGATIONS
# for the three scored regulations. Additional regulations can be seeded for
# report context but won't affect scores until COMPLIANCE_OBLIGATIONS is extended.

_REGULATIONS: list[dict] = [
    {
        "regulation_key": "UFLPA",
        "title": "Uyghur Forced Labor Prevention Act",
        "issuing_body": "US Department of Homeland Security",
        "geography": "US",
        "policy_theme": "supply_chain_due_diligence",
        "status": "effective",
        "publication_date": date(2021, 12, 23),
        "effective_date": date(2022, 6, 21),
        "summary": (
            "US federal law creating a rebuttable presumption that goods mined, produced, "
            "or manufactured in Xinjiang (China) — or by entities on the UFLPA Entity List — "
            "are made with forced labor and are therefore prohibited from US import. Applies "
            "to battery supply chains via natural graphite (significant Xinjiang processing), "
            "cobalt, and lithium. Importers must maintain clear supply chain traceability "
            "documentation to rebut the presumption."
        ),
        "metadata_json": {
            "enforcement_agency": "CBP (Customs and Border Protection)",
            "entity_list_url": "https://www.dhs.gov/uflpa-entity-list",
            "battery_materials_focus": ["Natural Graphite", "Cobalt", "Lithium"],
            "rebuttable_presumption": True,
        },
        # Producer-country enforcement risk.  Xinjiang-origin rebuttable
        # presumption — only CN-origin material falls under UFLPA scope;
        # non-CN producers aren't subject to the presumption.
        "geography_compliance_weights": {
            "CN": 1.0,
            "DEFAULT": 0.0,
        },
    },
    {
        "regulation_key": "IRA_DOMESTIC",
        "title": "IRA FEOC & Domestic Content Framework",
        "issuing_body": "US Department of the Treasury / IRS",
        "geography": "US",
        "policy_theme": "domestic_content_incentives",
        "status": "effective",
        "publication_date": date(2022, 8, 16),
        "effective_date": date(2023, 1, 1),
        "summary": (
            "The IRA-era Foreign Entity of Concern (FEOC) and domestic content framework governs "
            "eligibility for US federal clean-energy incentives. FEOCs include Chinese, Russian, "
            "North Korean, and Iranian government-controlled entities; the framework disqualifies "
            "battery components (FEOC battery component rule effective 2024) and critical minerals "
            "(FEOC critical mineral rule effective 2025) sourced from those entities. Major Chinese "
            "cell makers (CATL, BYD, CALB, Gotion) and upstream suppliers (Ganfeng, Tianqi, BTR, "
            "Huayou, CMOC) are presumed FEOCs. The §30D consumer clean-vehicle credit that was the "
            "original vehicle for this framework has been superseded; the FEOC and domestic content "
            "structure itself continues to govern remaining IRA-era incentives (notably the §45X "
            "advanced manufacturing production credit) and related Treasury/IRS guidance."
        ),
        "metadata_json": {
            "feoc_battery_component_rule_effective": "2024-01-01",
            "feoc_critical_mineral_rule_effective": "2025-01-01",
            "feoc_jurisdictions": ["CN", "RU", "KP", "IR"],
            "domestic_assembly_required": True,
            "consumer_credit_30d_status": "superseded",
            "scope_note": (
                "This row models the FEOC / domestic content framework as a durable policy "
                "signal. It is not tied to any single credit instrument; the §30D consumer "
                "credit has been superseded, but §45X and adjacent FEOC-gated incentives remain."
            ),
            # 2026-05-17 partner-direction: per-regulation severity override
            # raises IRA's effective-status severity from 0.70 (global
            # default) to 0.85, reflecting the FEOC-specific bite —
            # FEOC-sourced materials are functionally disqualified from
            # the §45X advanced manufacturing credit, which is closer to
            # banned-tier intensity than the global "effective" baseline
            # suggests.  Combined with the Idea-B conditional intersection
            # logic, this lets IRA fire harder for FEOC-exposed companies
            # while not over-penalizing domestic-only sourcing.
            "severity_overrides": {
                "effective": 0.85,
            },
            "severity_override_rationale": (
                "FEOC critical mineral rule (effective 2025) disqualifies "
                "FEOC-sourced battery materials from §45X advanced "
                "manufacturing credits — a categorical credit-loss "
                "consequence qualitatively closer to banned than to a "
                "garden-variety effective regulation."
            ),
        },
        # Producer-country enforcement risk.  FEOC binary per Treasury
        # guidance — materials originating from CN/RU/IR/KP disqualify
        # battery components from IRA §45X credits.  Non-FEOC producers
        # are out of scope.
        "geography_compliance_weights": {
            "CN": 1.0,
            "RU": 1.0,
            "IR": 1.0,
            "KP": 1.0,
            "DEFAULT": 0.0,
        },
    },
    {
        "regulation_key": "CRMA_2024",
        "title": "EU Critical Raw Materials Act",
        "issuing_body": "European Council",
        "geography": "EU",
        "policy_theme": "supply_chain_resilience",
        "status": "effective",
        "publication_date": date(2024, 3, 12),
        "effective_date": date(2024, 5, 23),
        "summary": (
            "EU regulation setting strategic targets to reduce single-country raw material "
            "dependency. Key benchmarks by 2030: domestic extraction ≥10% of EU consumption; "
            "domestic processing ≥40%; recycling ≥25%. No single third country may supply "
            ">65% of any strategic raw material. Battery-critical strategic materials include "
            "lithium, cobalt, nickel, manganese, natural graphite, and copper."
        ),
        "metadata_json": {
            "single_country_cap_pct": 65,
            "domestic_extraction_target_pct": 10,
            "domestic_processing_target_pct": 40,
            "recycling_target_pct": 25,
            "target_year": 2030,
        },
        # Producer-country enforcement risk.  CRMA's 65% single-country
        # cap targets concentration most heavily.  CN faces strongest
        # pressure (REE, graphite, refined Li/Co/Ni dominance); CD
        # secondary (cobalt concentration).  DEFAULT 0.3 captures that
        # any supplier inherits some EU diversification pressure.
        "geography_compliance_weights": {
            "CN": 1.0,
            "CD": 0.5,
            "DEFAULT": 0.3,
        },
    },
    {
        "regulation_key": "EU_CBAM",
        "title": "EU Carbon Border Adjustment Mechanism",
        "issuing_body": "European Commission",
        "geography": "EU",
        "policy_theme": "carbon_pricing",
        "status": "effective",
        "publication_date": date(2023, 5, 17),
        "effective_date": date(2023, 10, 1),
        "summary": (
            "Carbon pricing mechanism applied to imports of specified carbon-intensive products "
            "into the EU. Transitional reporting phase October 2023 – December 2025. Full CBAM "
            "certificates required from January 2026. Covers: cement, iron and steel, aluminium, "
            "fertilisers, electricity, and hydrogen. Copper is included under the aluminium "
            "sector expansion from 2026. Directly relevant to copper supply chains."
        ),
        "metadata_json": {
            "transitional_phase_end": "2025-12-31",
            "full_implementation": "2026-01-01",
            "covered_sectors": ["iron_steel", "aluminium", "cement", "fertilisers", "electricity", "hydrogen", "copper"],
        },
        # Producer-country enforcement risk.  Carbon-border certificates
        # bite hardest where grid carbon intensity is highest.  CN coal-
        # heavy grid → high-carbon Al / steel / Cu.  IN similar carbon
        # profile.  RU high-carbon refining footprint.  DEFAULT 0.3
        # captures baseline certificate cost for cleaner producers.
        "geography_compliance_weights": {
            "CN": 1.0,
            "IN": 1.0,
            "RU": 0.8,
            "DEFAULT": 0.3,
        },
    },
    {
        "regulation_key": "SEC_CLIMATE_2024",
        "title": "SEC Climate-Related Disclosure Rule",
        "issuing_body": "US Securities and Exchange Commission",
        "geography": "US",
        "policy_theme": "climate_disclosure",
        "status": "proposed",
        "publication_date": date(2024, 3, 6),
        "effective_date": date(2025, 1, 1),
        "summary": (
            "SEC rule requiring US-listed companies to disclose climate-related risks, Scope 1 "
            "and Scope 2 GHG emissions, and material Scope 3 emissions. Relevant to battery supply "
            "chain companies listed on US exchanges. As of early 2025, implementation stayed by "
            "federal court pending legal challenge. Status: effective but contested."
        ),
        "metadata_json": {
            "scope_1_2_required": True,
            "scope_3_required_for_large_accelerated_filers": True,
            "legal_status": "stayed_pending_court_review",
        },
    },

    # ── EU regulations (moved from eurlex.py manifest 2026-05-05) ────────────
    # eurlex.py is now an ingester only — fetches summaries / latest URL for
    # the regulations defined below via CELEX → regulation_key alias lookup.

    {
        "regulation_key": "EU_BATTERY_REG_2023",
        "title": "EU Battery Regulation (EU) 2023/1542 — batteries and waste batteries",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "policy_theme": "battery_lifecycle_compliance",
        "status": "effective",
        "publication_date": date(2023, 7, 28),
        "effective_date": date(2024, 8, 18),
        "summary": (
            "EU regulation establishing a comprehensive framework for batteries placed on the "
            "EU market: due-diligence obligations on cobalt, lithium, nickel, and natural "
            "graphite (Article 47); recycled-content minima rising through 2031–2036; battery "
            "passport for industrial / EV batteries from 2027; producer-responsibility scheme "
            "expansion. Non-compliance = EU market exclusion."
        ),
        "metadata_json": {
            "celex": "32023R1542",
            "due_diligence_effective": "2025-08-18",
            "recycled_content_lithium_2031_pct": 6,
            "recycled_content_cobalt_2031_pct": 16,
            "recycled_content_nickel_2031_pct": 6,
            "battery_passport_effective": "2027-02-18",
        },
        # Producer-country enforcement risk.  Article 47 due-diligence
        # obligations on Co/Li/Ni/Graphite.  CD (DRC cobalt) is the
        # canonical target.  CN dominates Co/Li refining + graphite
        # processing.  DEFAULT 0.5 reflects baseline due-diligence
        # exposure for all four scoped materials' producer countries
        # (AU, CA, CL, ID etc.) — the regulation has broad applicability,
        # but lower-risk jurisdictions face less scrutiny than the named
        # high-weight countries.
        "geography_compliance_weights": {
            "CD": 1.0,
            "CN": 0.8,
            "DEFAULT": 0.5,
        },
    },
    {
        "regulation_key": "EU_CSDDD",
        "title": "Corporate Sustainability Due Diligence Directive (EU) 2024/1760",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "policy_theme": "supply_chain_due_diligence",
        "status": "enacted",
        "publication_date": date(2024, 7, 5),
        "effective_date": date(2027, 7, 26),
        "summary": (
            "EU directive requiring large companies operating in the EU to identify, prevent, "
            "and address adverse human-rights and environmental impacts across their full value "
            "chain. Phased application 2027–2029 by company size. Civil liability for damages "
            "linked to due-diligence failures."
        ),
        "metadata_json": {
            "celex": "32024L1760",
            "first_phase_effective": "2027-07-26",
            "covers_value_chain": True,
            "civil_liability": True,
        },
        # Producer-country enforcement risk.  Broad human-rights +
        # environmental due diligence across full value chain.  CD
        # (DRC artisanal mining), MM (Myanmar REE/tin governance), and
        # CN (Xinjiang labour + Inner Mongolia environment) carry the
        # heaviest scrutiny.  DEFAULT 0.5 reflects baseline due-diligence
        # exposure for the regulation's broad applicability.
        "geography_compliance_weights": {
            "CD": 1.0,
            "MM": 1.0,
            "CN": 0.8,
            "DEFAULT": 0.5,
        },
    },
    {
        "regulation_key": "EU_CONFLICT_MINERALS",
        "title": "EU Conflict Minerals Regulation (EU) 2017/821",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "policy_theme": "responsible_sourcing",
        "status": "effective",
        "publication_date": date(2017, 5, 17),
        "effective_date": date(2021, 1, 1),
        "summary": (
            "EU regulation requiring importers of tin, tantalum, tungsten, and gold to perform "
            "supply-chain due diligence consistent with the OECD framework. Battery relevance is "
            "indirect (cobalt is not in scope) but the regulation establishes the OECD-based "
            "due-diligence baseline that downstream regulations (CSDDD, Battery Reg) reference."
        ),
        "metadata_json": {
            "celex": "32017R0821",
            "covered_minerals": ["tin", "tantalum", "tungsten", "gold"],
            "due_diligence_framework": "OECD_5_step",
        },
        # Producer-country enforcement risk.  3TG sourcing from CAHRAs
        # (Conflict-Affected and High-Risk Areas).  CD (Eastern DRC)
        # canonical conflict zone.  RW (Rwanda) smuggling concerns
        # over CD-origin minerals.  MM (Myanmar) tin from contested
        # areas.  DEFAULT 0.0 — non-CAHRA sources are out of scope.
        "geography_compliance_weights": {
            "CD": 1.0,
            "RW": 0.7,
            "MM": 0.7,
            "DEFAULT": 0.0,
        },
    },
    {
        "regulation_key": "EU_REACH_COBALT",
        "title": "REACH Regulation (EC) 1907/2006 — Cobalt SVHC restrictions",
        "issuing_body": "European Parliament and Council",
        "geography": "EU",
        "policy_theme": "chemicals_regulatory",
        "status": "effective",
        "publication_date": date(2006, 12, 18),
        "effective_date": date(2009, 6, 1),
        "summary": (
            "REACH regulation governs registration, evaluation, authorisation, and restriction "
            "of chemicals in the EU. Several cobalt compounds (cobalt sulfate, dichloride, "
            "nitrate, carbonate, acetate) are listed as Substances of Very High Concern (SVHC) "
            "under Annex XIV — requiring authorisation for use. Direct relevance to cobalt "
            "salts used in cathode-precursor manufacture."
        ),
        "metadata_json": {
            "celex": "32006R1907",
            "svhc_compounds": [
                "cobalt sulfate",
                "cobalt dichloride",
                "cobalt dinitrate",
                "cobalt carbonate",
                "cobalt diacetate",
            ],
        },
        # Producer-country enforcement risk.  REACH SVHC authorisation
        # applies to the USE of cobalt compounds in EU manufacturing —
        # it's an origin-agnostic use-side regulation.  All cobalt
        # producers face uniform downstream EU customer pressure
        # regardless of source; no named-country differentiation.
        "geography_compliance_weights": {
            "DEFAULT": 0.5,
        },
    },
]

# ---------------------------------------------------------------------------
# 2. Material scope per regulation
# ---------------------------------------------------------------------------
# Lists canonical material names that each regulation explicitly covers.

_MATERIAL_SCOPES: dict[str, list[dict]] = {
    "UFLPA": [
        # 2026-05-17 partner-direction recalibration: scope_type bumped
        # covered → banned to reflect UFLPA's actual enforcement bite.
        # The rebuttable-presumption + customs-seizure consequence is
        # qualitatively banned-tier — a Xinjiang-nexus shipment is
        # functionally prohibited from US import unless the importer
        # can affirmatively rebut the presumption.  Under the new
        # scope-severity-multiplier mechanism this raises UFLPA's
        # per-material event impact by ~1.9× compared to covered
        # (1.50× multiplier vs 0.80×).
        {"material": "Natural Graphite", "scope_type": "banned", "notes": "Significant Xinjiang processing concentration. Primary UFLPA battery enforcement focus."},
        {"material": "Cobalt", "scope_type": "banned", "notes": "DRC cobalt routed through Chinese processors with potential Xinjiang exposure."},
        {"material": "Lithium", "scope_type": "banned", "notes": "Some Chinese lithium processing in Xinjiang. Secondary enforcement focus."},
        # 2026-06-11 partner-direction (REE seed gap): China dominates light-
        # and heavy-REE processing — separation and oxide manufacture are
        # concentrated in Inner Mongolia, Sichuan, Jiangxi, and some Xinjiang
        # adjacents.  Xinjiang-nexus sourcing is a real (if secondary) UFLPA
        # enforcement path for REE-derived inputs in traction motors and
        # electronics.  Aggregate REE covers bundle-attributed events; the
        # four named magnet REEs travel separately for events that name a
        # specific element.
        {"material": "Rare Earth Elements", "scope_type": "banned", "notes": "Chinese REE separation/processing has Xinjiang-adjacent exposure (Inner Mongolia, Sichuan, Jiangxi)."},
        {"material": "Neodymium", "scope_type": "banned", "notes": "Traction-motor magnet REE; Chinese processing dominance creates UFLPA exposure."},
        {"material": "Praseodymium", "scope_type": "banned", "notes": "Traction-motor magnet REE; Chinese processing dominance creates UFLPA exposure."},
        {"material": "Dysprosium", "scope_type": "banned", "notes": "Heavy-REE magnet additive; Chinese processing dominance creates UFLPA exposure."},
        {"material": "Terbium", "scope_type": "banned", "notes": "Heavy-REE magnet additive; Chinese processing dominance creates UFLPA exposure."},
        # 2026-06-11 broader seed-coverage sweep: Xinjiang concentration is
        # direct and significant for three more battery-relevant materials.
        # Silicon (Anode Grade) is the largest gap — Xinjiang produces
        # 40%+ of global polysilicon and the UFLPA Withhold Release Order
        # on Hoshine Silicon Industry (June 2021) is the textbook UFLPA
        # action on a battery-adjacent material.  Xinjiang aluminum
        # production and ferromanganese / electrolytic-manganese-metal
        # output are both real but secondary to Silicon.
        {"material": "Silicon (Anode Grade)", "scope_type": "banned", "notes": "Xinjiang produces 40%+ of global polysilicon; Hoshine Silicon WRO (June 2021) is the textbook UFLPA action on battery-adjacent material."},
        {"material": "Aluminum", "scope_type": "banned", "notes": "Xinjiang is a major Chinese aluminum-smelting province; coal-fueled smelters there are direct UFLPA enforcement targets."},
        {"material": "Manganese", "scope_type": "banned", "notes": "Xinjiang ferromanganese and electrolytic manganese metal production create UFLPA exposure for battery-grade Mn supply chains."},
    ],
    "IRA_DOMESTIC": [
        {"material": "Lithium", "scope_type": "covered", "notes": "Critical mineral — FEOC sourcing disqualifies from tax credit."},
        {"material": "Cobalt", "scope_type": "covered", "notes": "Critical mineral under FEOC rules."},
        {"material": "Nickel", "scope_type": "covered", "notes": "Critical mineral under FEOC rules."},
        {"material": "Manganese", "scope_type": "covered", "notes": "Critical mineral under FEOC rules."},
        {"material": "Natural Graphite", "scope_type": "covered", "notes": "Battery component input — FEOC restrictions apply."},
        # 2026-06-11 partner-direction (REE seed gap): Nd, Pr, Dy, and Tb
        # are explicit US critical minerals under DOE/USGS designations, and
        # NdFeB magnets are core components of EV traction motors that
        # qualify under §45X manufacturing credits.  FEOC sourcing of these
        # REEs disqualifies the resulting magnet from credit eligibility.
        # Aggregate REE covers events that attribute generically.
        {"material": "Rare Earth Elements", "scope_type": "covered", "notes": "REE supply chain in §45X scope for traction-motor magnet production."},
        {"material": "Neodymium",   "scope_type": "covered", "notes": "Critical mineral — NdFeB magnet feedstock; FEOC sourcing disqualifies §45X credit."},
        {"material": "Praseodymium", "scope_type": "covered", "notes": "Critical mineral — NdFeB magnet feedstock; FEOC sourcing disqualifies §45X credit."},
        {"material": "Dysprosium",  "scope_type": "covered", "notes": "Critical mineral — high-temperature magnet additive; FEOC sourcing disqualifies §45X credit."},
        {"material": "Terbium",     "scope_type": "covered", "notes": "Critical mineral — magnet additive; FEOC sourcing disqualifies §45X credit."},
        # 2026-06-11 broader seed-coverage sweep: launch-list materials
        # that are §45X-eligible battery components but were missing from
        # the IRA scope.  Aluminum, Copper, and Silicon are all on the
        # current US critical-minerals list; Phosphate (Battery Grade) is
        # the LFP cathode active material directly named in §45X eligible
        # component lists.  FEOC-sourced versions of any of these
        # disqualify the resulting battery / component from §45X credits.
        {"material": "Aluminum",                "scope_type": "covered", "notes": "US critical mineral; Al collector foil + casings are §45X-eligible battery components."},
        {"material": "Copper",                  "scope_type": "covered", "notes": "Added to US critical mineral list (USGS 2024); Cu current-collector foil is a §45X-eligible battery component."},
        {"material": "Phosphate (Battery Grade)", "scope_type": "covered", "notes": "LFP cathode active material is explicitly §45X-eligible; FEOC-sourced phosphate disqualifies LFP-cathode credit."},
        {"material": "Silicon (Anode Grade)",   "scope_type": "covered", "notes": "US critical mineral; Si anode active material is a §45X-eligible battery component."},
    ],
    # NOTE: ``EU_BATTERY_REG_2023`` material scopes are defined further down
    # in this dict (after CRMA / CBAM) so the strategic-raw-material context
    # is grouped with the other EU regulations.  The old ``EU_BATTERY_REG``
    # key (without year suffix) was deprecated 2026-05-05 — use
    # ``EU_BATTERY_REG_2023`` to match COMPLIANCE_OBLIGATIONS in
    # regulatory_risk.py.
    "CRMA_2024": [
        # CRMA Annex II: Strategic Raw Materials.  "Strategic" is a higher
        # designation than "critical" (Annex I); EU member states face binding
        # 2030 benchmarks against this list (≥10% domestic extraction, ≥40%
        # processing, ≥15% recycling).  scope_type "strategic_raw_material"
        # maps to severity multiplier 1.00 (between covered and restricted).
        # 2026-05-17 partner-direction: magnet REEs (Nd, Pr, Dy, Tb)
        # promoted to "restricted" because they're already in active
        # violation of CRMA's 65% single-country cap (China dominates
        # heavy-REE processing), so the regulation's bite is real now,
        # not aspirational.  Per Idea B's threshold-trigger logic,
        # these materials will additionally fire synthetic violation
        # events at scoring time for China-exposed companies.
        # Battery / EV drivetrain
        {"material": "Lithium",                "scope_type": "strategic_raw_material"},
        {"material": "Cobalt",                 "scope_type": "strategic_raw_material"},
        {"material": "Nickel",                 "scope_type": "strategic_raw_material"},
        {"material": "Manganese",              "scope_type": "strategic_raw_material"},
        {"material": "Natural Graphite",       "scope_type": "strategic_raw_material"},
        {"material": "Copper",                 "scope_type": "strategic_raw_material"},
        # 2026-06-11 broader seed-coverage sweep: CRMA Annex II names
        # "Bauxite / alumina / aluminium" together as a strategic raw
        # material — current seed missed it.  EU has binding 2030 benchmarks
        # against Al concentration (China + Russia dominate primary smelting).
        {"material": "Aluminum",               "scope_type": "strategic_raw_material",
         "notes": "CRMA Annex II Strategic Raw Material (bauxite/alumina/aluminium)."},
        # LFP-chemistry materials (2026-05-17 addition)
        {"material": "Phosphate (Battery Grade)", "scope_type": "strategic_raw_material",
         "notes": "LFP cathode active material; closes phosphate coverage gap identified in v2 walkthrough."},
        # Industrial / electronics
        {"material": "Boron",                  "scope_type": "strategic_raw_material"},
        {"material": "Gallium",                "scope_type": "strategic_raw_material"},
        {"material": "Germanium",              "scope_type": "strategic_raw_material"},
        {"material": "Magnesium",              "scope_type": "strategic_raw_material"},
        {"material": "Silicon (Anode Grade)",  "scope_type": "strategic_raw_material"},
        {"material": "Titanium",               "scope_type": "strategic_raw_material"},
        {"material": "Tungsten",               "scope_type": "strategic_raw_material"},
        {"material": "Niobium",                "scope_type": "strategic_raw_material"},
        {"material": "Bismuth",                "scope_type": "strategic_raw_material"},
        # Platinum-group metals (fuel cell catalysts, sensors)
        {"material": "Platinum-Group Metals",  "scope_type": "strategic_raw_material"},
        # REE bundled — for events that attribute to "REE" generically
        # without naming a specific element.  2026-05-17 addition closes
        # a gap where bundle-attributed events received no CRMA signal.
        {"material": "Rare Earth Elements",    "scope_type": "strategic_raw_material",
         "notes": "Bundled REE canonical — fires when an event attributes to REEs generically without naming a specific element."},
        # Magnet rare-earth elements (EV traction motors) — restricted-tier
        # per 2026-05-17 partner-direction.  China dominates heavy-REE
        # processing; these are already in CRMA-cap-violation today.
        {"material": "Neodymium",              "scope_type": "restricted",
         "notes": "EV traction-motor magnet REE; China dominates processing — already exceeds CRMA single-country cap."},
        {"material": "Praseodymium",           "scope_type": "restricted",
         "notes": "EV traction-motor magnet REE; China dominates processing."},
        {"material": "Dysprosium",             "scope_type": "restricted",
         "notes": "Heavy REE for high-temperature magnet additive; China dominates."},
        {"material": "Terbium",                "scope_type": "restricted",
         "notes": "Heavy REE for magnet additive; China dominates."},
    ],
    "EU_CBAM": [
        # CBAM coverage (see metadata_json.covered_sectors): cement,
        # iron/steel, aluminium, fertilisers, electricity, hydrogen, plus
        # copper from 2026 under the aluminium-sector expansion.  We seed
        # the battery-relevant materials only.
        # 2026-05-17 partner-direction addition: Iron Ore (LFP Grade)
        # added to reflect CBAM's iron/steel coverage.  LFP-chemistry
        # companies have meaningful CBAM exposure on the iron-ore side
        # that was missing from the previous seed.
        {"material": "Aluminum",              "scope_type": "covered"},
        {"material": "Nickel",                "scope_type": "covered"},
        {"material": "Copper",                "scope_type": "covered", "notes": "Copper included under aluminium sector extension from 2026."},
        {"material": "Iron Ore (LFP Grade)",  "scope_type": "covered", "notes": "CBAM covers iron and steel imports — relevant to LFP-chemistry supply chains."},
    ],
    "EU_BATTERY_REG_2023": [
        # 2026-05-17 partner-direction recalibration: scope_type bumped
        # disclosure_required → restricted.  The recycled-content
        # thresholds (lithium 6%, cobalt 16%, nickel 6% by 2031) are
        # product-acceptability bars, not just reporting requirements —
        # batteries that fail the thresholds cannot be placed on the EU
        # market.  That's a binding restriction on material composition,
        # which is the textbook definition of "restricted" scope_type.
        # Under the new scope-severity-multiplier mechanism this raises
        # per-material event impact by ~2.2× (1.10× multiplier vs 0.50×).
        #
        # 2026-05-17 partner-direction additions:
        #   * Phosphate (Battery Grade): LFP cathode-active material —
        #     closes phosphate coverage gap identified in v2 walkthrough
        #   * Iron Ore (LFP Grade): LFP cathode-active material —
        #     parallel to phosphate addition above
        #   * Aluminum: cell-component (current-collector foil, cell
        #     casings, module structural) — `covered` not `restricted`
        #     because aluminum has no recycled-content threshold under
        #     the regulation; it's covered by the broader battery-
        #     component framework but isn't bound by per-material
        #     composition requirements.
        {"material": "Lithium",                   "scope_type": "restricted", "notes": "Battery passport + due diligence from 2025. Recycled-content target 6% by 2031."},
        {"material": "Cobalt",                    "scope_type": "restricted", "notes": "Battery passport + due diligence from 2025. Recycled-content target 16% by 2031."},
        {"material": "Nickel",                    "scope_type": "restricted", "notes": "Battery passport + due diligence from 2025. Recycled-content target 6% by 2031."},
        {"material": "Manganese",                 "scope_type": "restricted", "notes": "Battery passport + due diligence from 2025."},
        {"material": "Natural Graphite",          "scope_type": "restricted", "notes": "Battery passport + due diligence from 2025."},
        {"material": "Phosphate (Battery Grade)", "scope_type": "restricted", "notes": "LFP cathode active material — regulated under battery-component composition framework."},
        {"material": "Iron Ore (LFP Grade)",      "scope_type": "restricted", "notes": "LFP cathode active material — parallel to phosphate."},
        {"material": "Aluminum",                  "scope_type": "covered",    "notes": "Cell-component coverage (cathode current-collector foil, casings, module structural) — no per-material recycled-content threshold."},
        # 2026-06-11 broader seed-coverage sweep: parallel to Aluminum
        # above.  Copper foil is the anode-side current collector; Silicon
        # is increasingly used in anode active material blends.  Both are
        # in the battery-component scope of EU 2023/1542 but without per-
        # material composition thresholds, so 'covered' (not 'restricted').
        {"material": "Copper",                "scope_type": "covered", "notes": "Anode-side current-collector foil — in battery-component scope; no per-material composition threshold."},
        {"material": "Silicon (Anode Grade)", "scope_type": "covered", "notes": "Silicon anode active material — in battery-component scope as cells shift to higher Si content."},
    ],
    "EU_CSDDD": [
        {"material": "Cobalt",           "scope_type": "disclosure_required", "notes": "Mandatory human-rights / environmental due diligence across supply chain."},
        {"material": "Lithium",          "scope_type": "disclosure_required"},
        {"material": "Natural Graphite", "scope_type": "disclosure_required"},
        # 2026-06-11 partner-direction (REE seed gap): CSDDD due-diligence
        # obligation extends across the EU's strategic-raw-material supply
        # chain (CRMA-mapped) — REE supply chain is one of the highest-
        # concentration single-country dependencies and a top CSDDD audit
        # target.  Includes aggregate REE for bundle-attributed events
        # plus the four named magnet elements.
        {"material": "Rare Earth Elements", "scope_type": "disclosure_required", "notes": "Strategic raw material per CRMA — CSDDD due diligence extends to REE supply chain (mining + processing + magnet manufacture)."},
        {"material": "Neodymium",   "scope_type": "disclosure_required", "notes": "Traction-motor magnet REE — CSDDD due-diligence target."},
        {"material": "Praseodymium", "scope_type": "disclosure_required", "notes": "Traction-motor magnet REE — CSDDD due-diligence target."},
        {"material": "Dysprosium",  "scope_type": "disclosure_required", "notes": "Heavy-REE magnet additive — CSDDD due-diligence target."},
        {"material": "Terbium",     "scope_type": "disclosure_required", "notes": "Heavy-REE magnet additive — CSDDD due-diligence target."},
        # 2026-06-11 broader seed-coverage sweep: CSDDD applies to large EU
        # companies' supply chains broadly.  These six battery materials
        # all have well-documented supply-chain due-diligence concerns
        # (forced labor, environmental, indigenous-land, artisanal mining)
        # that are explicit CSDDD audit targets.  Each entry names the
        # primary concentration of concern.
        {"material": "Aluminum",                "scope_type": "disclosure_required", "notes": "Bauxite from Guinea (50%+ global bauxite share) — forced-labor and indigenous-land due-diligence concerns."},
        {"material": "Copper",                  "scope_type": "disclosure_required", "notes": "DRC artisanal copper-cobalt sourcing — child-labor and environmental due-diligence concerns."},
        {"material": "Nickel",                  "scope_type": "disclosure_required", "notes": "Indonesian laterite nickel HPAL (Sulawesi) — environmental and indigenous-land due-diligence concerns."},
        {"material": "Manganese",               "scope_type": "disclosure_required", "notes": "South African and Gabonese manganese — labor-rights due-diligence concerns."},
        {"material": "Phosphate (Battery Grade)", "scope_type": "disclosure_required", "notes": "Western Sahara phosphate (~70% of MA phosphate exports) — occupation / indigenous-rights due-diligence concerns."},
        {"material": "Silicon (Anode Grade)",   "scope_type": "disclosure_required", "notes": "Xinjiang polysilicon — UFLPA-overlapping forced-labor due-diligence concerns."},
    ],
    "EU_CONFLICT_MINERALS": [
        # 2026-05-17 partner-direction recalibration: the regulation
        # formally covers 3TG (tin, tantalum, tungsten, gold).  The
        # previous Cobalt-only entry was a domain-judgment stretch
        # ("cobalt commonly co-mined with conflict zones") that didn't
        # survive a strict-text-of-the-regulation read.  Removed and
        # replaced with the actually-regulated materials.  Gold is
        # omitted because it's not in our battery-launch materials
        # roster — add when partner extends materials coverage.
        {"material": "Tin",      "scope_type": "disclosure_required", "notes": "3TG — OECD 5-step due diligence required for EU importers."},
        {"material": "Tantalum", "scope_type": "disclosure_required", "notes": "3TG — OECD 5-step due diligence required for EU importers."},
        {"material": "Tungsten", "scope_type": "disclosure_required", "notes": "3TG — OECD 5-step due diligence required for EU importers."},
    ],
    "EU_REACH_COBALT": [
        {"material": "Cobalt", "scope_type": "restricted", "notes": "Several cobalt compounds classified as Substances of Very High Concern (SVHC) under REACH Annex XIV."},
    ],
}

# ---------------------------------------------------------------------------
# 3. Geography scope per regulation
# ---------------------------------------------------------------------------
# scope_type: jurisdiction (regulation applies here) | targeted_country (regulation targets imports from here)

_GEOGRAPHY_SCOPES: dict[str, list[dict]] = {
    "UFLPA": [
        {"country_code": "US", "scope_type": "jurisdiction"},
        {"country_code": "CN", "scope_type": "targeted_country"},  # Xinjiang origin
    ],
    "IRA_DOMESTIC": [
        {"country_code": "US", "scope_type": "jurisdiction"},
        {"country_code": "CN", "scope_type": "targeted_country"},
        {"country_code": "RU", "scope_type": "targeted_country"},
        {"country_code": "KP", "scope_type": "targeted_country"},
        {"country_code": "IR", "scope_type": "targeted_country"},
    ],
    "EU_BATTERY_REG_2023": [
        {"country_code": "EU", "scope_type": "jurisdiction"},
    ],
    "CRMA_2024": [
        # 2026-05-17 partner-direction (Section 4 walkthrough): expanded
        # targeted_country list to reflect CRMA's actual single-non-EU-
        # country supply-concentration concerns.  CRMA caps single-
        # source dependency at 65% per strategic material; the bite
        # concentrates wherever a non-EU country dominates a strategic
        # supply chain:
        #   CN — graphite, REEs, processed nickel, refined cobalt
        #   ID — primary nickel mining (60%+ global share)
        #   CL — lithium (alongside Australia)
        #   CD — cobalt mining (DRC dominates raw cobalt)
        #   RU — PGMs, refined nickel
        # Combined with Idea-B's threshold-trigger logic, exposure to
        # any of these countries above 65% on a CRMA-listed material
        # fires a synthetic violation event in the regulatory pillar.
        {"country_code": "EU", "scope_type": "jurisdiction"},
        {"country_code": "CN", "scope_type": "targeted_country"},
        {"country_code": "ID", "scope_type": "targeted_country"},
        {"country_code": "CL", "scope_type": "targeted_country"},
        {"country_code": "CD", "scope_type": "targeted_country"},
        {"country_code": "RU", "scope_type": "targeted_country"},
    ],
    "EU_CBAM": [
        # 2026-05-17 partner-direction (Option C / Section 4 walkthrough):
        # removed CN + RU from targeted_country.  CBAM is a universal
        # trade-flow regulation that applies to ALL non-EU imports of
        # covered sectors, not just imports from named countries.
        # Listing only CN/RU caused Idea-B's conditional intersection
        # logic to incorrectly downweight CBAM for companies sourcing
        # from other non-EU origins (Vietnam, US, Brazil, etc.).  With
        # no targeted_country, CBAM applies at full bite to every
        # company in scope.
        {"country_code": "EU", "scope_type": "jurisdiction"},
    ],
    "EU_CSDDD": [
        {"country_code": "EU", "scope_type": "jurisdiction"},
        {"country_code": "CD", "scope_type": "targeted_country"},  # DRC — cobalt
        {"country_code": "CN", "scope_type": "targeted_country"},  # supply-chain visibility focus
    ],
    "EU_CONFLICT_MINERALS": [
        {"country_code": "EU", "scope_type": "jurisdiction"},
        {"country_code": "CD", "scope_type": "targeted_country"},  # DRC — original 3TG focus area
    ],
    "EU_REACH_COBALT": [
        {"country_code": "EU", "scope_type": "jurisdiction"},
    ],
    "SEC_CLIMATE_2024": [
        {"country_code": "US", "scope_type": "jurisdiction"},
    ],
}

# ---------------------------------------------------------------------------
# 4. Company regulation exposures
# ---------------------------------------------------------------------------
# compliance_status: compliant | partial | non_compliant | unknown
#
# Key scoring logic (regulatory_risk.py):
#   UFLPA:          25 pt uplift if non_compliant / partial / unknown
#   EU_BATTERY_REG_2023: 20 pt uplift if non_compliant / partial / unknown
#   IRA_DOMESTIC:   15 pt uplift if non_compliant / partial / unknown
#
# Only rows with status ≠ "compliant" generate uplift. Compliant rows are
# recorded for completeness and auditability.

_COMPANY_EXPOSURES: list[dict] = [

    # ─── UFLPA ──────────────────────────────────────────────────────────────
    # Natural graphite from China is the primary enforcement vector for batteries.
    # Gotion High-tech: on US national security concern list; Michigan factory
    # subject to federal scrutiny. Marked non_compliant as most exposed.

    {"company": "Gotion High-tech",       "regulation": "UFLPA", "status": "non_compliant",
     "reason": "On US DoD Section 1260H list. Michigan factory subject to federal review. Xinjiang-linked graphite supply chain documented."},

    {"company": "BTR New Energy",          "regulation": "UFLPA", "status": "partial",
     "reason": "World's largest graphite anode producer. CN operations include regions with Xinjiang-origin feedstock. Has not provided sufficient traceability documentation for US imports."},

    {"company": "ShanShan Corporation",    "regulation": "UFLPA", "status": "partial",
     "reason": "Second largest graphite anode producer. CN supply chain with unverified Xinjiang graphite exposure."},

    {"company": "Ganfeng Lithium",         "regulation": "UFLPA", "status": "partial",
     "reason": "Has operations and processing in CN including regions with Xinjiang-origin materials. Supplies major cell makers for US-market vehicles."},

    {"company": "Huayou Cobalt",           "regulation": "UFLPA", "status": "partial",
     "reason": "CN-based cobalt/lithium processor with DRC sourcing routed through Chinese processing. Supply chain Xinjiang exposure not fully excluded."},

    {"company": "CATL",                    "regulation": "UFLPA", "status": "partial",
     "reason": "Under active US scrutiny. CN graphite anode supply chain has unverified Xinjiang exposure. Rebuttable presumption applies to CN-manufactured cells."},

    {"company": "BYD",                     "regulation": "UFLPA", "status": "partial",
     "reason": "CN-manufactured batteries with domestic graphite supply chain. Xinjiang-origin feedstock traceability not publicly documented."},

    {"company": "CALB Group",              "regulation": "UFLPA", "status": "partial",
     "reason": "CN cell maker with domestic graphite supply chain. Limited traceability documentation for US import purposes."},

    {"company": "FinDreams Battery",       "regulation": "UFLPA", "status": "partial",
     "reason": "BYD subsidiary. CN manufacturing with domestic graphite supply chain. Same traceability gap as parent."},

    {"company": "CMOC Group",              "regulation": "UFLPA", "status": "partial",
     "reason": "CN-listed mining company with DRC operations but CN-based corporate structure. CN processing exposure to UFLPA scrutiny."},

    # US OEMs with Chinese graphite in supply chain
    {"company": "Tesla",                   "regulation": "UFLPA", "status": "partial",
     "reason": "CATL LFP cells (CN-manufactured) in Model 3 standard range. Panasonic/LGES cells have CN-sourced graphite anodes. Active Syrah Vidalia offtake is a partial mitigation."},

    {"company": "General Motors",          "regulation": "UFLPA", "status": "partial",
     "reason": "Ultium cells via LGES JV. Anode graphite supply chain routes through CN processors. Working to qualify non-Xinjiang graphite sources for IRA compliance."},

    {"company": "Ford Motor Company",      "regulation": "UFLPA", "status": "partial",
     "reason": "BlueOval SK (SK On) cells have CN-sourced graphite anodes. CATL LFP supply agreement for some models adds further CN supply chain exposure."},

    {"company": "Rivian Automotive",       "regulation": "UFLPA", "status": "partial",
     "reason": "Samsung SDI cells with CN graphite anode supply chain. Single cell supplier limits supply chain diversification options."},

    {"company": "Lucid Group",             "regulation": "UFLPA", "status": "unknown",
     "reason": "Cell supplier not publicly disclosed. CN graphite supply chain assumed given market structure. Traceability documentation not publicly available."},

    # Non-US OEMs with US sales
    {"company": "Hyundai Motor Company",   "regulation": "UFLPA", "status": "unknown",
     "reason": "Sells vehicles in US market. Korean cell makers (LGES, SK On) have CN graphite supply chains. Traceability documentation under development."},

    {"company": "Kia Corporation",         "regulation": "UFLPA", "status": "unknown",
     "reason": "US market participant. Cell suppliers (SK On, LGES, SDI) have CN graphite upstream. Traceability gap."},

    {"company": "Toyota Motor Corporation","regulation": "UFLPA", "status": "unknown",
     "reason": "US market participant. PPES/Panasonic supply chain has CN graphite anode exposure. Mitigation efforts via Toyota's supply chain programme underway."},

    {"company": "Honda Motor Company",     "regulation": "UFLPA", "status": "unknown",
     "reason": "US market participant. LGES JV has CN graphite supply chain exposure."},

    {"company": "Nissan Motor Company",    "regulation": "UFLPA", "status": "unknown",
     "reason": "US market participant. Envision AESC supply chain includes CN graphite."},

    {"company": "Subaru Corporation",      "regulation": "UFLPA", "status": "unknown",
     "reason": "US market participant via Solterra. Shares Toyota/PPES supply chain with CN graphite exposure."},

    {"company": "Volkswagen Group",        "regulation": "UFLPA", "status": "unknown",
     "reason": "US market participant. CATL and Gotion supply chain (Gotion is flagged) creates elevated UFLPA exposure."},

    {"company": "BMW Group",               "regulation": "UFLPA", "status": "unknown",
     "reason": "US market participant. Samsung SDI and CATL cells have CN graphite supply chains."},

    {"company": "Stellantis",              "regulation": "UFLPA", "status": "unknown",
     "reason": "US market participant. Samsung SDI cells have CN graphite upstream."},

    # Korean cell makers supplying US-market vehicles
    {"company": "LG Energy Solution",      "regulation": "UFLPA", "status": "partial",
     "reason": "Cell maker supplying US-market vehicles (Tesla, GM, Hyundai). Anode graphite predominantly CN-sourced. Working to qualify US and non-CN graphite sources."},

    {"company": "Samsung SDI",             "regulation": "UFLPA", "status": "partial",
     "reason": "Supplies Rivian, BMW (US models). CN graphite anode supply chain. Traceability programme in progress."},

    {"company": "SK On",                   "regulation": "UFLPA", "status": "partial",
     "reason": "BlueOval SK JV with Ford. CN graphite in anode supply chain. IRA compliance programme addressing this."},

    {"company": "Panasonic Energy",        "regulation": "UFLPA", "status": "partial",
     "reason": "Supplies Tesla Gigafactory Nevada. CN graphite anode supply chain. Syrah Vidalia offtake partially mitigates but doesn't eliminate exposure."},

    # ─── IRA_DOMESTIC ───────────────────────────────────────────────────────
    # Exposures are scored against the IRA FEOC / domestic content framework,
    # not against any single credit instrument (the §30D consumer credit that
    # originally anchored this framework has been superseded). FEOC entities
    # (Chinese-/Russian-state-linked) are non_compliant — their components or
    # minerals structurally disqualify downstream participants from FEOC-gated
    # IRA incentives (e.g. §45X, related Treasury guidance). OEMs sourcing from
    # FEOC supply chains are partial — managing transition.

    {"company": "CATL",                    "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated entity under US Treasury guidance. Cells manufactured by CATL disqualify downstream participants from FEOC-gated IRA incentives. Separate Shenzhen-listed from CN government oversight is insufficient for FEOC exemption."},

    {"company": "BYD",                     "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated. Chinese state-linked OEM and battery manufacturer. Vehicles and battery components disqualify from FEOC-gated IRA incentives."},

    {"company": "CALB Group",              "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated Chinese cell maker. Components disqualify downstream vehicles from FEOC-gated IRA incentives."},

    {"company": "Gotion High-tech",        "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated. On DoD Section 1260H list. VW minority stake does not change FEOC status. Michigan facility subject to ongoing federal review."},

    {"company": "FinDreams Battery",       "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "BYD subsidiary. FEOC by inheritance from parent. Same disqualification applies."},

    {"company": "BTR New Energy",          "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated CN anode material producer. Graphite from BTR disqualifies downstream vehicles from FEOC-gated IRA incentives when traced."},

    {"company": "ShanShan Corporation",    "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated CN anode/cathode producer. Materials disqualify downstream sourcing from FEOC-gated IRA incentives when traced."},

    {"company": "Ganfeng Lithium",         "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated CN lithium refiner. Lithium compounds from Ganfeng disqualify downstream sourcing from FEOC-gated IRA incentives under the critical mineral rule effective 2025."},

    {"company": "Tianqi Lithium",          "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated CN lithium refiner. Lithium compounds disqualify downstream sourcing from FEOC-gated IRA incentives."},

    {"company": "Huayou Cobalt",           "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated CN cobalt/lithium processor. Materials disqualify downstream sourcing from FEOC-gated IRA incentives."},

    {"company": "CMOC Group",              "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "FEOC-designated CN mining company. Cobalt and copper from CMOC disqualify downstream sourcing from FEOC-gated IRA incentives."},

    # OEMs — partial: in transition, managing FEOC exposure
    {"company": "Tesla",                   "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Model 3 standard range uses CATL LFP (FEOC) — those vehicles fail FEOC qualification. 4680-cell models are domestic but use CN graphite. Actively transitioning to qualify more models. Partial compliance."},

    {"company": "General Motors",          "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Ultium/LGES cells are non-FEOC, but lithium and graphite sourcing includes CN-origin material not yet qualified. Working toward full mineral traceability for 2025 FEOC mineral rules."},

    {"company": "Ford Motor Company",      "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "BlueOval SK (SK On) cells are non-FEOC. However CATL LFP supply agreement for Explorer EV (non-US sold) and CN-sourced graphite create partial disqualification risk. Complex vehicle-by-vehicle compliance."},

    {"company": "Rivian Automotive",       "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Samsung SDI cells are non-FEOC at the cell level. However upstream CN graphite and mineral sourcing creates mineral-rule disqualification risk from 2025. Amazon van fleet complicates commercial vehicle credit status."},

    {"company": "Lucid Group",             "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Samsung SDI cells — non-FEOC at entity level. Saudi PIF ownership does not create FEOC exposure (SA is not a FEOC jurisdiction under IRA). Partial status reflects upstream CN mineral sourcing risk under the 2025 critical mineral rules."},

    {"company": "Hyundai Motor Company",   "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Korean cell makers (LGES, SK On) are non-FEOC at cell level. Georgia factory qualifies for domestic assembly. Upstream CN mineral sourcing creates partial disqualification from 2025 FEOC mineral rules."},

    {"company": "Kia Corporation",         "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Korean cell supply is non-FEOC at cell level. CN mineral sourcing upstream creates partial exposure. Kia Georgia plant (planned) would satisfy domestic assembly."},

    {"company": "Toyota Motor Corporation","regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "PPES cells (Toyota/Panasonic JV) non-FEOC. But CN graphite in anode supply creates mineral-rule exposure. Toyota working to qualify AU/CA mineral sources."},

    {"company": "Honda Motor Company",     "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "LGES JV cells non-FEOC. CN mineral upstream creates exposure. Honda Ohio assembly satisfies domestic requirement for some models."},

    {"company": "Volkswagen Group",        "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "SK On supply is non-FEOC. But Gotion stake and Gotion's Michigan factory create significant IRA exposure risk. CN graphite supply chain adds further mineral-rule issues."},

    {"company": "Stellantis",              "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Samsung SDI cells (non-FEOC) supply StarPlus Energy. CN mineral upstream exposure remains. Some Stellantis models use ACC (EU JV) — no US production."},

    # Cell makers as suppliers
    {"company": "LG Energy Solution",      "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Korean cell maker — not FEOC at entity level. But CN-sourced graphite and minerals create downstream credit disqualification risk for OEM customers under mineral rules."},

    {"company": "Samsung SDI",             "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Korean entity — not FEOC. Indiana StarPlus Energy factory qualifies for domestic component credit. CN mineral sourcing upstream creates partial exposure."},

    {"company": "SK On",                   "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Korean entity — not FEOC. US factories (BlueOval SK) qualify for domestic component credit. CN graphite upstream exposure for mineral rules."},

    {"company": "Panasonic Energy",        "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Japanese entity — not FEOC. Gigafactory Nevada qualifies for domestic credit. CN graphite exposure partially offset by Syrah Vidalia offtake. Partial compliance."},

    # Russian entities — Russia is a FEOC jurisdiction under IRA (alongside CN, KP, IR).
    # Materials sourced from Russian state-linked entities disqualify IRA credits.
    {"company": "Norilsk Nickel",          "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "Russian state-linked entity. Russia is an IRA FEOC jurisdiction. Nickel and copper from Norilsk disqualify downstream sourcing from FEOC-gated IRA incentives. Structural non-compliance — no transition pathway while under Russian sovereign control."},

    # Chinese OEMs — FEOC-linked by CN state ownership structure
    {"company": "Geely Auto Group",        "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "Chinese OEM with CN government ownership links (Zhejiang SASAC). FEOC-linked entity. Does not currently sell in US market directly but FEOC status applies to entity-level designation."},

    {"company": "Zeekr",                   "regulation": "IRA_DOMESTIC", "status": "non_compliant",
     "reason": "Geely subsidiary. FEOC-linked by inheritance from Geely/Zhejiang SASAC ownership chain. Does not currently sell in US market but FEOC entity designation applies."},

    # Korean material producers supplying US-market batteries
    {"company": "POSCO Holdings",          "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "South Korean entity — not FEOC. But POSCO's cathode precursor and anode businesses (POSCO Future M) source CN minerals upstream. Critical mineral FEOC rules from 2025 create partial disqualification risk for downstream vehicles."},

    {"company": "POSCO Future M",          "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "POSCO subsidiary, cathode active materials and anode producer. Supplies Korean cell makers for US-assembled vehicles. CN-origin lithium and graphite in upstream supply create IRA mineral-rule exposure from 2025."},

    {"company": "Ecopro BM",               "regulation": "IRA_DOMESTIC", "status": "partial",
     "reason": "Korean cathode active materials producer. Supplies Samsung SDI and SK On for US-market vehicles. Upstream CN mineral sourcing (lithium, nickel precursors) creates IRA critical mineral rule exposure from 2025."},

    # ─── EU_BATTERY_REG_2023 ────────────────────────────────────────────────
    # All EU market participants are in scope. Requirements phase in 2024–2031.
    # Most companies are "partial" — the regulatory timeline is still unfolding
    # and full compliance documentation is not yet required for all provisions.
    # Northvolt and Chinese companies have elevated exposure given specific issues.

    {"company": "Northvolt",               "regulation": "EU_BATTERY_REG_2023", "status": "non_compliant",
     "reason": "Bankruptcy filing (November 2024) jeopardizes ability to meet EU Battery Reg compliance documentation requirements. Supply chain transparency commitments made pre-bankruptcy may not survive restructuring."},

    {"company": "CATL",                    "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "Sells into EU market. Subject to due diligence requirements for cobalt, graphite, lithium, nickel from 2025. Battery passport implementation underway but not complete. CN sourcing structure increases scrutiny."},

    {"company": "Gotion High-tech",        "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU market participant (Valencia gigafactory planned). Due diligence requirements for CN-sourced materials create compliance challenge given existing US regulatory scrutiny."},

    {"company": "Envision AESC",           "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "Operates UK (Sunderland) and France gigafactories for EU/UK market. In scope for full EU Battery Reg requirements. Due diligence implementation underway."},

    {"company": "LG Energy Solution",      "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "Major EU cell supplier (Poland gigafactory). Subject to full EU Battery Reg compliance including due diligence, carbon footprint declaration, and battery passport. Working toward compliance."},

    {"company": "Samsung SDI",             "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU cell supplier (Hungary factory). Full compliance timeline in progress. Due diligence for CN-sourced materials is primary challenge."},

    {"company": "SK On",                   "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU cell supplier (SKBA JV with VW in Germany). Battery Reg compliance programme underway alongside financial restructuring."},

    {"company": "Umicore",                 "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "Belgian CAM producer — EU-based, in scope. Positioned as EU Battery Reg-compliant supplier but full documentation for all provisions not yet complete."},

    {"company": "BASF",                    "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU-based CAM producer. In scope for supplier due diligence obligations. RU nickel supply chain exposure creates Norilsk-related disclosure complexity."},

    # EU OEMs
    {"company": "Volkswagen Group",        "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU OEM selling into EU market. Battery passport and due diligence requirements in scope. PowerCo SE designed partly to create EU Battery Reg-compliant domestic supply chain."},

    {"company": "BMW Group",               "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU OEM. One of the more advanced OEMs on cobalt responsible sourcing (direct smelter MOUs). Full EU Battery Reg compliance documentation still in progress."},

    {"company": "Mercedes-Benz Group",     "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU OEM. Due diligence requirements and battery passport implementation underway. CATL supply chain creates CN-sourcing transparency challenges."},

    {"company": "Stellantis",              "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU OEM (NL HQ, multi-country manufacturing). Full EU Battery Reg compliance programme in progress."},

    {"company": "Volvo Car Group",         "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU OEM (Sweden). Selling into EU market. CATL supply chain requires due diligence documentation. Battery passport programme underway."},

    {"company": "Polestar Automotive",     "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU-HQ OEM. High-sustainability brand positioning means elevated scrutiny of compliance documentation. CATL CN manufacturing creates due diligence challenge."},

    # Non-EU OEMs selling into EU
    {"company": "Toyota Motor Corporation","regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "Sells into EU market. PPES supply chain requires EU Battery Reg due diligence documentation. Compliance programme underway."},

    {"company": "Honda Motor Company",     "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU market participant. Battery Reg compliance documentation in progress."},

    {"company": "Hyundai Motor Company",   "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU market participant. Czech Republic assembly plant. Battery Reg compliance programme underway."},

    {"company": "Kia Corporation",         "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU market participant. Slovak assembly plant. Battery Reg compliance programme underway."},

    {"company": "Nissan Motor Company",    "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "EU market participant. UK Sunderland factory (AESC cells). Battery Reg compliance underway."},

    # Norilsk — Russian entity creates specific disclosure challenges
    {"company": "Norilsk Nickel",          "regulation": "EU_BATTERY_REG_2023", "status": "non_compliant",
     "reason": "Russian entity subject to EU sanctions monitoring. Nickel from Norilsk cannot be sourced without triggering EU Battery Reg supply chain due diligence flags. Effectively excluded from compliant EU supply chains."},

    # Chinese cell makers with EU presence
    {"company": "BYD",                     "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "Selling into EU vehicle market. Hungarian factory under construction. Due diligence requirements for CN-sourced battery materials in scope. Battery passport implementation will be required."},

    # Korean cathode/anode material producers supplying EU-market batteries
    {"company": "Ecopro BM",               "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "Cathode active materials producer supplying Samsung SDI (Hungary) and SK On (Germany JV) for EU-market batteries. In scope for supply chain due diligence provisions from 2025. Due diligence programme in development."},

    {"company": "POSCO Future M",          "regulation": "EU_BATTERY_REG_2023", "status": "partial",
     "reason": "Cathode and anode materials producer. Supplies EU-market cell production. Subject to supply chain due diligence for cobalt, lithium, nickel, graphite sourcing from 2025."},

    # ─── CRMA_2024 (informational, no scoring weight yet) ───────────────────

    {"company": "Ganfeng Lithium",         "regulation": "CRMA_2024", "status": "unknown",
     "reason": "CN lithium refiner supplying EU market. CRMA single-country concentration cap (>65%) directly applies — China supplies >80% of EU refined lithium."},

    {"company": "Tianqi Lithium",          "regulation": "CRMA_2024", "status": "unknown",
     "reason": "CN lithium refiner with 22% SQM stake. Part of the CN supply chain concentration that CRMA targets."},

    {"company": "BTR New Energy",          "regulation": "CRMA_2024", "status": "unknown",
     "reason": "CN graphite anode leader. China supplies ~70% of EU graphite anode materials — breaches CRMA 65% single-country cap."},

    {"company": "CATL",                    "regulation": "CRMA_2024", "status": "unknown",
     "reason": "CN cell maker with EU gigafactory (Hungary). Subject to CRMA as a critical material consumer. CN concentration in upstream supply chain is a CRMA focus."},

    {"company": "Volkswagen Group",        "regulation": "CRMA_2024", "status": "partial",
     "reason": "EU OEM. CRMA requires stress-testing of supply chain for single-country dependency breaches. VW's CN cell sourcing and CN graphite supply chain create concentration risk."},

    {"company": "BMW Group",               "regulation": "CRMA_2024", "status": "partial",
     "reason": "EU OEM. Same CN concentration issues. BMW has the most advanced upstream due diligence but CRMA compliance requires formal assessment and reporting."},

    # ─── EU_CBAM (copper supply chain exposure) ─────────────────────────────

    {"company": "Glencore",                "regulation": "EU_CBAM", "status": "partial",
     "reason": "Major copper producer/trader exporting to EU. CBAM will apply to copper from 2026. Carbon intensity of DRC operations requires certification."},

    {"company": "Freeport-McMoRan",        "regulation": "EU_CBAM", "status": "partial",
     "reason": "Copper producer with EU-bound trade flows. CBAM carbon intensity reporting required from 2026."},

    {"company": "CMOC Group",              "regulation": "EU_CBAM", "status": "partial",
     "reason": "DRC copper producer. EU CBAM applies to copper imports. CN-controlled entity may face additional EU scrutiny."},

    {"company": "Norilsk Nickel",          "regulation": "EU_CBAM", "status": "non_compliant",
     "reason": "Russian entity under EU sanctions. EU import of Russian copper is effectively prohibited under current sanctions regime — CBAM compliance is moot as import is blocked."},
]


# ---------------------------------------------------------------------------
# Seed function
# ---------------------------------------------------------------------------

def seed_regulations(session: Session, *, force: bool = False) -> dict[str, int]:
    """Upsert regulations, material scopes, geography scopes, and company exposures.

    Four-pass operation:
      Pass 1: Upsert regulations by regulation_key (always updates mutable fields)
      Pass 2: Upsert regulation_material_scope (needs regulation_id + material_id)
      Pass 3: Upsert regulation_geography_scope (needs regulation_id)
      Pass 4: Upsert company_regulation_exposure (needs company_id + regulation_id)

    Normal run (force=False):
        Passes 2–3 skip existing rows — their values in the DB are authoritative.
        Pass 4 always updates changed fields (compliance status can change over time).

    Force run (force=True):
        Passes 2–3 overwrite ``scope_type`` and ``notes`` on existing scope rows
        with the current seed values.  Use after correcting scope classifications
        in ``_MATERIAL_SCOPES`` or ``_GEOGRAPHY_SCOPES``.
        Does NOT delete scope rows that exist in the DB but not in the seed.

    Returns counts per table.
    """
    # Pre-load lookups
    companies: dict[str, object] = {
        row.canonical_name: row.id
        for row in session.scalars(select(Company))
    }
    materials: dict[str, int] = {
        row.canonical_name: row.id
        for row in session.scalars(select(Material))
    }

    reg_ids: dict[str, int] = {}
    regs_inserted = regs_updated = 0
    mat_scopes_inserted = mat_scopes_updated = mat_scopes_skipped = 0
    geo_scopes_inserted = geo_scopes_updated = geo_scopes_skipped = 0
    exposures_inserted = exposures_updated = exposures_skipped = 0

    # ── Pass 1: Regulations ──────────────────────────────────────────────────
    # Every row produced by this seed is stamped ``verified=True`` and carries
    # ``metadata_json.seed_version`` so the DB has provenance for which seed
    # revision the row came from.  Scoring filters on ``verified=True`` —
    # only rows we curated here (or in seed_regulation_aliases) feed the
    # regulatory pillar.
    for reg_data in _REGULATIONS:
        key = reg_data["regulation_key"]
        # Defensive copy so we don't mutate the module-level dict.
        payload = dict(reg_data)
        # Merge seed_version into metadata_json (preserve other fields).
        meta = dict(payload.get("metadata_json") or {})
        meta["seed_version"] = _SEED_VERSION
        payload["metadata_json"] = meta
        payload["verified"] = True

        existing = session.scalar(
            select(Regulation).where(Regulation.regulation_key == key)
        )
        if existing is None:
            reg = Regulation(**payload)
            session.add(reg)
            session.flush()
            reg_ids[key] = reg.id
            log.info("seed_regulations.inserted", regulation_key=key,
                     seed_version=_SEED_VERSION)
            regs_inserted += 1
        else:
            reg_ids[key] = existing.id
            # Update mutable fields (including verified + metadata_json so
            # rows pre-dating the verified-flag rollout get upgraded on
            # re-seed).  geography_compliance_weights added 2026-06-06 —
            # existing DB rows had NULL JSONB for all 8 partner-tier
            # regulations, so the merge-on-re-seed path needs to update
            # this field too (otherwise the weights would only flow
            # through to fresh-environment first-time inserts).
            for field in ("title", "summary", "status", "effective_date",
                          "metadata_json", "verified",
                          "geography_compliance_weights"):
                if getattr(existing, field) != payload.get(field):
                    setattr(existing, field, payload.get(field))
            regs_updated += 1
            log.debug("seed_regulations.regulation_exists", regulation_key=key)

    session.flush()

    # ── Pass 2: Material scopes ──────────────────────────────────────────────
    for reg_key, scope_list in _MATERIAL_SCOPES.items():
        reg_id = reg_ids.get(reg_key)
        if reg_id is None:
            log.warning("seed_regulations.material_scope_missing_reg", regulation_key=reg_key)
            continue
        for entry in scope_list:
            mat_id = materials.get(entry["material"])
            if mat_id is None:
                log.warning("seed_regulations.material_not_found", material=entry["material"])
                mat_scopes_skipped += 1
                continue
            existing = session.scalar(
                select(RegulationMaterialScope).where(
                    RegulationMaterialScope.regulation_id == reg_id,
                    RegulationMaterialScope.material_id == mat_id,
                )
            )
            if existing is None:
                session.add(RegulationMaterialScope(
                    regulation_id=reg_id,
                    material_id=mat_id,
                    scope_type=entry.get("scope_type", "covered"),
                    notes=entry.get("notes"),
                ))
                mat_scopes_inserted += 1
            else:
                if force:
                    existing.scope_type = entry.get("scope_type", "covered")
                    existing.notes = entry.get("notes")
                    mat_scopes_updated += 1
                else:
                    mat_scopes_skipped += 1

    session.flush()

    # ── Pass 3: Geography scopes ─────────────────────────────────────────────
    for reg_key, geo_list in _GEOGRAPHY_SCOPES.items():
        reg_id = reg_ids.get(reg_key)
        if reg_id is None:
            continue
        for entry in geo_list:
            existing = session.scalar(
                select(RegulationGeographyScope).where(
                    RegulationGeographyScope.regulation_id == reg_id,
                    RegulationGeographyScope.country_code == entry["country_code"],
                )
            )
            if existing is None:
                session.add(RegulationGeographyScope(
                    regulation_id=reg_id,
                    country_code=entry["country_code"],
                    scope_type=entry.get("scope_type", "jurisdiction"),
                ))
                geo_scopes_inserted += 1
            else:
                if force:
                    existing.scope_type = entry.get("scope_type", "jurisdiction")
                    geo_scopes_updated += 1
                else:
                    geo_scopes_skipped += 1

    session.flush()

    # ── Pass 4: Company exposures ────────────────────────────────────────────
    for entry in _COMPANY_EXPOSURES:
        company_id = companies.get(entry["company"])
        reg_id = reg_ids.get(entry["regulation"])

        if company_id is None:
            log.warning(
                "seed_regulations.company_not_found",
                company=entry["company"],
                regulation=entry["regulation"],
            )
            exposures_skipped += 1
            continue

        if reg_id is None:
            log.warning(
                "seed_regulations.regulation_not_found",
                regulation=entry["regulation"],
                company=entry["company"],
            )
            exposures_skipped += 1
            continue

        existing = session.scalar(
            select(CompanyRegulationExposure).where(
                CompanyRegulationExposure.company_id == company_id,
                CompanyRegulationExposure.regulation_id == reg_id,
            )
        )

        if existing is None:
            session.add(CompanyRegulationExposure(
                company_id=company_id,
                regulation_id=reg_id,
                compliance_status=entry["status"],
                exposure_reason=entry.get("reason"),
                assessed_at=_ASSESSED_AT,
            ))
            log.info(
                "seed_regulations.exposure_inserted",
                company=entry["company"],
                regulation=entry["regulation"],
                status=entry["status"],
            )
            exposures_inserted += 1
        else:
            changed = False
            for field, value in {
                "compliance_status": entry["status"],
                "exposure_reason": entry.get("reason"),
                "assessed_at": _ASSESSED_AT,
            }.items():
                if getattr(existing, field) != value:
                    setattr(existing, field, value)
                    changed = True
            if changed:
                log.info(
                    "seed_regulations.exposure_updated",
                    company=entry["company"],
                    regulation=entry["regulation"],
                    status=entry["status"],
                )
            exposures_updated += 1

    session.commit()

    result = {
        "regulations_inserted": regs_inserted,
        "regulations_updated": regs_updated,
        "material_scopes_inserted": mat_scopes_inserted,
        "material_scopes_updated": mat_scopes_updated,
        "material_scopes_skipped": mat_scopes_skipped,
        "geography_scopes_inserted": geo_scopes_inserted,
        "geography_scopes_updated": geo_scopes_updated,
        "geography_scopes_skipped": geo_scopes_skipped,
        "exposures_inserted": exposures_inserted,
        "exposures_updated": exposures_updated,
        "exposures_skipped": exposures_skipped,
    }
    log.info("seed_regulations.done", **result)
    return result
