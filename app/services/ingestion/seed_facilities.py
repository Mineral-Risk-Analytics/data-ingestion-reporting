"""Curated seed data for known physical facilities across the EV battery supply chain.

Static reference data — not ingested or scraped. Manually verified from public
sources (company investor relations, press releases, Benchmark Mineral Intelligence,
Wood Mackenzie, USGS, Bloomberg NEF).

Scope
-----
This module covers:
  - Cell factories (gigafactories)
  - OEM EV assembly / pack plants
  - Recycling facilities
  - R&D and HQ facilities

It does NOT cover:
  - Mines            ← populated by ``bdi-ingest ingest-gem``
  - Refineries       ← populated by ``bdi-ingest ingest-gem``

The mining and refinery records that previously existed in this file have been
removed. GEM (Global Energy Monitor) provides a richer, quarterly-updated
dataset for extraction and processing facilities with capacity data that feeds
the operational scoring pillar. Re-adding mines/refineries here would create
duplicates and undermine the GEM dedup logic.

Idempotent: deduplicates on (company_id, facility_type, country, city). A facility
with the same company, type, country, and city is partially updated on re-run;
``city=None`` entries are compared NULL-safe (two NULL cities are treated as equal).

Run order:
    bdi-ingest seed-companies    # companies must exist first
    bdi-ingest seed-facilities   # this module (cell factories, pack plants, recycling)
    bdi-ingest ingest-gem        # separately, for mines + refineries
"""

from __future__ import annotations

import structlog
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.facility import CompanyFacility, Facility

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Curated facility data
# ---------------------------------------------------------------------------

_FACILITIES: list[dict] = [
    # ── CELL MAKER GIGAFACTORIES ─────────────────────────────────────────

    # CATL
    {"company_canonical_name": "CATL", "facility_type": "cell_factory", "country": "CN", "region": "Fujian", "city": "Ningde", "status": "operating", "capacity_notes": "~180 GWh/yr; original home base", "data_source": "manual"},
    {"company_canonical_name": "CATL", "facility_type": "cell_factory", "country": "CN", "region": "Jiangsu", "city": "Liyang", "status": "operating", "capacity_notes": "50 GWh/yr", "data_source": "manual"},
    {"company_canonical_name": "CATL", "facility_type": "cell_factory", "country": "CN", "region": "Sichuan", "city": "Yibin", "status": "operating", "capacity_notes": "100 GWh/yr; Time3 base", "data_source": "manual"},
    {"company_canonical_name": "CATL", "facility_type": "cell_factory", "country": "CN", "region": "Guangdong", "city": "Zhaoqing", "status": "operating", "capacity_notes": "50 GWh/yr", "data_source": "manual"},
    {"company_canonical_name": "CATL", "facility_type": "cell_factory", "country": "DE", "region": "Thuringia", "city": "Erfurt", "status": "operating", "capacity_notes": "14 GWh/yr Phase 1; 100 GWh/yr long-term target. Supplies BMW, VW, Stellantis.", "latitude": 50.9936, "longitude": 10.9116, "data_source": "manual"},
    {"company_canonical_name": "CATL", "facility_type": "cell_factory", "country": "HU", "region": "Hajdú-Bihar", "city": "Debrecen", "status": "under_construction", "capacity_notes": "100 GWh/yr planned; Phase 1 commissioning 2025.", "latitude": 47.5316, "longitude": 21.6273, "data_source": "manual"},

    # LG Energy Solution
    {"company_canonical_name": "LG Energy Solution", "facility_type": "cell_factory", "country": "KR", "region": "North Chungcheong", "city": "Ochang", "status": "operating", "capacity_notes": "~30 GWh/yr; primary Korea base", "data_source": "manual"},
    {"company_canonical_name": "LG Energy Solution", "facility_type": "cell_factory", "country": "CN", "region": "Jiangsu", "city": "Nanjing", "status": "operating", "capacity_notes": "~30 GWh/yr; supplies Tesla China and local OEMs", "data_source": "manual"},
    {"company_canonical_name": "LG Energy Solution", "facility_type": "cell_factory", "country": "PL", "region": "Lower Silesia", "city": "Wrocław", "status": "operating", "capacity_notes": "~65 GWh/yr; largest LGES plant outside Korea. Supplies Audi, Volvo, GM.", "latitude": 51.1079, "longitude": 17.0385, "data_source": "manual"},
    {"company_canonical_name": "LG Energy Solution", "facility_type": "cell_factory", "country": "US", "region": "Michigan", "city": "Holland", "status": "operating", "capacity_notes": "Ultium Cells JV with GM; ~35 GWh/yr", "data_source": "manual"},
    {"company_canonical_name": "LG Energy Solution", "facility_type": "cell_factory", "country": "US", "region": "Tennessee", "city": "Spring Hill", "status": "operating", "capacity_notes": "Ultium Cells JV with GM; ~35 GWh/yr", "data_source": "manual"},
    {"company_canonical_name": "LG Energy Solution", "facility_type": "cell_factory", "country": "US", "region": "Arizona", "city": "Queen Creek", "status": "under_construction", "capacity_notes": "L-H Battery Company JV with Honda; ~40 GWh/yr planned 2026", "data_source": "manual"},

    # Samsung SDI
    {"company_canonical_name": "Samsung SDI", "facility_type": "cell_factory", "country": "KR", "region": "South Gyeongsang", "city": "Ulsan", "status": "operating", "capacity_notes": "~25 GWh/yr; EV and ESS cells", "data_source": "manual"},
    {"company_canonical_name": "Samsung SDI", "facility_type": "cell_factory", "country": "HU", "region": "Pest", "city": "Göd", "status": "operating", "capacity_notes": "~30 GWh/yr; supplies BMW Leipzig, Stellantis, Rivian.", "latitude": 47.6857, "longitude": 19.1330, "data_source": "manual"},
    {"company_canonical_name": "Samsung SDI", "facility_type": "cell_factory", "country": "US", "region": "Indiana", "city": "Kokomo", "status": "under_construction", "capacity_notes": "StarPlus Energy JV with Stellantis; ~33 GWh/yr planned 2025", "latitude": 40.4865, "longitude": -86.1336, "data_source": "manual"},
    {"company_canonical_name": "Samsung SDI", "facility_type": "cell_factory", "country": "US", "region": "Indiana", "city": "New Carlisle", "status": "planned", "capacity_notes": "Samsung SDI standalone; ~34 GWh/yr planned 2027. Supplies Stellantis.", "data_source": "manual"},

    # SK On
    {"company_canonical_name": "SK On", "facility_type": "cell_factory", "country": "KR", "region": "South Chungcheong", "city": "Seosan", "status": "operating", "capacity_notes": "~20 GWh/yr; primary Korea base", "data_source": "manual"},
    {"company_canonical_name": "SK On", "facility_type": "cell_factory", "country": "HU", "region": "Komárom-Esztergom", "city": "Komárom", "status": "operating", "capacity_notes": "~30 GWh/yr; supplies VW Group, Hyundai.", "latitude": 47.7444, "longitude": 18.1188, "data_source": "manual"},
    {"company_canonical_name": "SK On", "facility_type": "cell_factory", "country": "US", "region": "Georgia", "city": "Commerce", "status": "operating", "capacity_notes": "BlueOval SK JV with Ford; ~43 GWh/yr. Supplies F-150 Lightning, Mustang Mach-E.", "latitude": 34.2037, "longitude": -83.4571, "data_source": "manual"},
    {"company_canonical_name": "SK On", "facility_type": "cell_factory", "country": "US", "region": "Kentucky", "city": "Glendale", "status": "operating", "capacity_notes": "BlueOval SK JV with Ford; ~43 GWh/yr", "latitude": 37.6423, "longitude": -85.9027, "data_source": "manual"},

    # Panasonic Energy
    {"company_canonical_name": "Panasonic Energy", "facility_type": "cell_factory", "country": "JP", "region": "Hyogo", "city": "Kasai", "status": "operating", "capacity_notes": "Cylindrical NCA cells; primary Japan production", "data_source": "manual"},
    {"company_canonical_name": "Panasonic Energy", "facility_type": "cell_factory", "country": "US", "region": "Nevada", "city": "Sparks", "status": "operating", "capacity_notes": "Gigafactory Nevada; ~38 GWh/yr; exclusive Tesla supplier. 2170 and 4680 cells.", "latitude": 39.5378, "longitude": -119.4431, "data_source": "manual"},
    {"company_canonical_name": "Panasonic Energy", "facility_type": "cell_factory", "country": "US", "region": "Kansas", "city": "De Soto", "status": "under_construction", "capacity_notes": "~30 GWh/yr planned 2025; prismatic cells for EV market beyond Tesla.", "latitude": 38.9736, "longitude": -95.0285, "data_source": "manual"},

    # BYD / FinDreams Battery
    {"company_canonical_name": "FinDreams Battery", "facility_type": "cell_factory", "country": "CN", "region": "Guangdong", "city": "Shenzhen", "status": "operating", "capacity_notes": "Primary Blade Battery production; LFP", "data_source": "manual"},
    {"company_canonical_name": "FinDreams Battery", "facility_type": "cell_factory", "country": "CN", "region": "Shaanxi", "city": "Xi'an", "status": "operating", "capacity_notes": "Major Blade Battery base; ~80 GWh/yr", "data_source": "manual"},
    {"company_canonical_name": "FinDreams Battery", "facility_type": "cell_factory", "country": "CN", "region": "Hunan", "city": "Changsha", "status": "operating", "capacity_notes": "LFP cells; ~30 GWh/yr", "data_source": "manual"},

    # Envision AESC
    {"company_canonical_name": "Envision AESC", "facility_type": "cell_factory", "country": "GB", "region": "Tyne and Wear", "city": "Sunderland", "status": "operating", "capacity_notes": "~2 GWh/yr existing; 35 GWh/yr gigafactory planned. Primary Nissan Leaf supply.", "latitude": 54.9143, "longitude": -1.3836, "data_source": "manual"},
    {"company_canonical_name": "Envision AESC", "facility_type": "cell_factory", "country": "US", "region": "Tennessee", "city": "Smyrna", "status": "operating", "capacity_notes": "~8 GWh/yr; supplies Nissan Leaf US production.", "latitude": 35.9829, "longitude": -86.5186, "data_source": "manual"},
    {"company_canonical_name": "Envision AESC", "facility_type": "cell_factory", "country": "FR", "region": "Hauts-de-France", "city": "Douai", "status": "under_construction", "capacity_notes": "40 GWh/yr planned; supplies Renault Ampere.", "latitude": 50.3714, "longitude": 3.0797, "data_source": "manual"},

    # Prime Planet and Energy Solutions
    {"company_canonical_name": "Prime Planet and Energy Solutions", "facility_type": "cell_factory", "country": "JP", "region": "Aichi", "city": "Toyota City", "status": "operating", "capacity_notes": "Prismatic NMC and LFP; primary supply for Toyota and Lexus hybrids/EVs.", "data_source": "manual"},
    {"company_canonical_name": "Prime Planet and Energy Solutions", "facility_type": "cell_factory", "country": "JP", "region": "Hyogo", "city": "Himeji", "status": "operating", "capacity_notes": "Cylindrical cells for hybrids", "data_source": "manual"},

    # Northvolt
    {"company_canonical_name": "Northvolt", "facility_type": "cell_factory", "country": "SE", "region": "Västerbotten", "city": "Skellefteå", "status": "mothballed", "capacity_notes": "Filed for bankruptcy November 2024. Gigafactory capacity 60 GWh/yr planned; ~5 GWh/yr actual at closure. BMW, VW primary customers affected.", "latitude": 64.7507, "longitude": 20.9528, "data_source": "manual"},
    {"company_canonical_name": "Northvolt", "facility_type": "r_and_d", "country": "SE", "region": "Västmanland", "city": "Västerås", "status": "operating", "capacity_notes": "R&D and pilot line; likely to survive restructuring", "data_source": "manual"},

    # CALB Group
    {"company_canonical_name": "CALB Group", "facility_type": "cell_factory", "country": "CN", "region": "Jiangsu", "city": "Changzhou", "status": "operating", "capacity_notes": "~30 GWh/yr; NMC and LFP. Supplies Li Auto, Geely, Neta.", "data_source": "manual"},
    {"company_canonical_name": "CALB Group", "facility_type": "cell_factory", "country": "CN", "region": "Guangdong", "city": "Huizhou", "status": "operating", "capacity_notes": "~20 GWh/yr", "data_source": "manual"},

    # Gotion High-tech
    {"company_canonical_name": "Gotion High-tech", "facility_type": "cell_factory", "country": "CN", "region": "Anhui", "city": "Hefei", "status": "operating", "capacity_notes": "~30 GWh/yr; LFP primary. VW holds ~26%.", "data_source": "manual"},
    {"company_canonical_name": "Gotion High-tech", "facility_type": "cell_factory", "country": "US", "region": "Michigan", "city": "Big Rapids", "status": "under_construction", "capacity_notes": "~40 GWh/yr planned 2027. Subject to US national security review.", "data_source": "manual"},
    {"company_canonical_name": "Gotion High-tech", "facility_type": "cell_factory", "country": "DE", "region": "Rhineland-Palatinate", "city": "Kaiserslautern", "status": "planned", "capacity_notes": "~20 GWh/yr planned; partnership with VW", "data_source": "manual"},

    # ── OEM EV ASSEMBLY / PACK PLANTS ────────────────────────────────────

    # Tesla
    {"company_canonical_name": "Tesla", "facility_type": "pack_plant", "country": "US", "region": "California", "city": "Fremont", "status": "operating", "capacity_notes": "~550,000 vehicles/yr. Model S, 3, X, Y. Also cell module assembly.", "latitude": 37.4924, "longitude": -121.9624, "data_source": "manual"},
    {"company_canonical_name": "Tesla", "facility_type": "pack_plant", "country": "US", "region": "Texas", "city": "Austin", "status": "operating", "capacity_notes": "~250,000 vehicles/yr. Cybertruck, Model Y. 4680 cell integration.", "latitude": 30.2240, "longitude": -97.6183, "data_source": "manual"},
    {"company_canonical_name": "Tesla", "facility_type": "pack_plant", "country": "CN", "region": "Shanghai", "city": "Shanghai", "status": "operating", "capacity_notes": "~750,000 vehicles/yr. Model 3, Model Y. Largest Tesla plant globally.", "latitude": 30.8672, "longitude": 121.5900, "data_source": "manual"},
    {"company_canonical_name": "Tesla", "facility_type": "pack_plant", "country": "DE", "region": "Brandenburg", "city": "Grünheide", "status": "operating", "capacity_notes": "~250,000 vehicles/yr. Model Y. Supplies European market.", "latitude": 52.2740, "longitude": 13.8200, "data_source": "manual"},

    # Volkswagen Group
    {"company_canonical_name": "Volkswagen Group", "facility_type": "pack_plant", "country": "DE", "region": "Saxony", "city": "Zwickau", "status": "operating", "capacity_notes": "~300,000 vehicles/yr; fully converted to BEV. ID.3, ID.4, ID.5, Audi Q4, Cupra Born.", "latitude": 50.7172, "longitude": 12.4977, "data_source": "manual"},
    {"company_canonical_name": "Volkswagen Group", "facility_type": "pack_plant", "country": "DE", "region": "Lower Saxony", "city": "Emden", "status": "operating", "capacity_notes": "ID.4, ID.7 production", "latitude": 53.3569, "longitude": 7.1890, "data_source": "manual"},
    {"company_canonical_name": "Volkswagen Group", "facility_type": "pack_plant", "country": "US", "region": "Tennessee", "city": "Chattanooga", "status": "operating", "capacity_notes": "~150,000 vehicles/yr. ID.4 for US market.", "latitude": 35.0457, "longitude": -85.3097, "data_source": "manual"},
    {"company_canonical_name": "Volkswagen Group", "facility_type": "pack_plant", "country": "CN", "region": "Shanghai", "city": "Anting", "status": "operating", "capacity_notes": "SAIC-VW JV. ID.3, ID.4 for China market.", "data_source": "manual"},

    # PowerCo SE
    {"company_canonical_name": "PowerCo SE", "facility_type": "cell_factory", "country": "DE", "region": "Lower Saxony", "city": "Salzgitter", "status": "under_construction", "capacity_notes": "40 GWh/yr planned; VW's first in-house gigafactory. Unified prismatic cell format.", "latitude": 52.1565, "longitude": 10.3744, "data_source": "manual"},
    {"company_canonical_name": "PowerCo SE", "facility_type": "cell_factory", "country": "ES", "region": "Valencia", "city": "Sagunto", "status": "planned", "capacity_notes": "40 GWh/yr planned 2026", "latitude": 39.6830, "longitude": -0.2714, "data_source": "manual"},
    {"company_canonical_name": "PowerCo SE", "facility_type": "cell_factory", "country": "CA", "region": "Ontario", "city": "St. Thomas", "status": "planned", "capacity_notes": "~90 GWh/yr originally planned; status uncertain post VW restructuring 2024", "data_source": "manual"},

    # BMW Group
    {"company_canonical_name": "BMW Group", "facility_type": "pack_plant", "country": "DE", "region": "Saxony", "city": "Leipzig", "status": "operating", "capacity_notes": "BMW iX, i4, i5. Primary EV plant. Cells from Samsung SDI (Göd).", "latitude": 51.3397, "longitude": 12.3731, "data_source": "manual"},
    {"company_canonical_name": "BMW Group", "facility_type": "pack_plant", "country": "DE", "region": "Bavaria", "city": "Dingolfing", "status": "operating", "capacity_notes": "BMW i7, iX5, competence centre for EV drivetrains. Cells from Samsung SDI.", "latitude": 48.6231, "longitude": 12.4999, "data_source": "manual"},
    {"company_canonical_name": "BMW Group", "facility_type": "pack_plant", "country": "DE", "region": "Bavaria", "city": "Munich", "status": "operating", "capacity_notes": "BMW i4, MINI Countryman EV", "latitude": 48.1775, "longitude": 11.5561, "data_source": "manual"},
    {"company_canonical_name": "BMW Group", "facility_type": "pack_plant", "country": "US", "region": "South Carolina", "city": "Spartanburg", "status": "operating", "capacity_notes": "Primarily ICE/PHEV X-series. Some PHEV battery packs.", "latitude": 34.9621, "longitude": -81.9540, "data_source": "manual"},
    {"company_canonical_name": "BMW Group", "facility_type": "pack_plant", "country": "CN", "region": "Liaoning", "city": "Shenyang", "status": "operating", "capacity_notes": "BMW Brilliance JV. iX3, i3 for China market. Cells from CATL.", "latitude": 41.7974, "longitude": 123.4328, "data_source": "manual"},

    # General Motors
    {"company_canonical_name": "General Motors", "facility_type": "pack_plant", "country": "US", "region": "Michigan", "city": "Hamtramck", "status": "operating", "capacity_notes": "Factory ZERO; Hummer EV, Silverado EV, Brightdrop. Ultium cells from LGES.", "latitude": 42.3965, "longitude": -83.0490, "data_source": "manual"},
    {"company_canonical_name": "General Motors", "facility_type": "pack_plant", "country": "US", "region": "Tennessee", "city": "Spring Hill", "status": "operating", "capacity_notes": "Cadillac Lyriq, Chevy Blazer EV. Ultium cells.", "latitude": 35.7512, "longitude": -86.9297, "data_source": "manual"},
    {"company_canonical_name": "General Motors", "facility_type": "pack_plant", "country": "US", "region": "Michigan", "city": "Orion Township", "status": "under_construction", "capacity_notes": "Chevy Silverado EV, Sierra EV. Ultium cells.", "data_source": "manual"},

    # Ford Motor Company
    {"company_canonical_name": "Ford Motor Company", "facility_type": "pack_plant", "country": "US", "region": "Michigan", "city": "Dearborn", "status": "operating", "capacity_notes": "F-150 Lightning production. BlueOval SK cells from Commerce GA.", "latitude": 42.3123, "longitude": -83.2096, "data_source": "manual"},
    {"company_canonical_name": "Ford Motor Company", "facility_type": "pack_plant", "country": "MX", "region": "Mexico State", "city": "Cuautitlán Izcalli", "status": "operating", "capacity_notes": "Mustang Mach-E. BlueOval SK cells.", "latitude": 19.6683, "longitude": -99.1819, "data_source": "manual"},
    {"company_canonical_name": "Ford Motor Company", "facility_type": "pack_plant", "country": "DE", "region": "North Rhine-Westphalia", "city": "Cologne", "status": "operating", "capacity_notes": "Explorer EV, Capri EV. CATL cells. Converted 2024.", "latitude": 50.9284, "longitude": 6.9083, "data_source": "manual"},

    # Hyundai Motor Company
    {"company_canonical_name": "Hyundai Motor Company", "facility_type": "pack_plant", "country": "US", "region": "Georgia", "city": "Ellabell", "status": "operating", "capacity_notes": "HMGMA; Ioniq 5, Ioniq 6, Ioniq 9. SK On cells. ~300,000 vehicles/yr capacity.", "latitude": 32.1562, "longitude": -81.4668, "data_source": "manual"},
    {"company_canonical_name": "Hyundai Motor Company", "facility_type": "pack_plant", "country": "KR", "region": "South Gyeongsang", "city": "Ulsan", "status": "operating", "capacity_notes": "Ioniq 5, 6. Primary Korea EV plant.", "latitude": 35.5384, "longitude": 129.3114, "data_source": "manual"},

    # Kia Corporation
    {"company_canonical_name": "Kia Corporation", "facility_type": "pack_plant", "country": "KR", "region": "Gyeonggi", "city": "Hwaseong", "status": "operating", "capacity_notes": "EV6, EV9. SK On and Samsung SDI cells.", "latitude": 37.1869, "longitude": 126.8237, "data_source": "manual"},
    {"company_canonical_name": "Kia Corporation", "facility_type": "pack_plant", "country": "US", "region": "Georgia", "city": "West Point", "status": "under_construction", "capacity_notes": "EV9, EV6. IRA-qualifying US production. Opening 2026.", "latitude": 32.8879, "longitude": -85.1833, "data_source": "manual"},

    # Toyota Motor Corporation
    {"company_canonical_name": "Toyota Motor Corporation", "facility_type": "pack_plant", "country": "JP", "region": "Aichi", "city": "Toyota City", "status": "operating", "capacity_notes": "Global HQ and primary manufacturing. Hybrids and BEV bZ series.", "data_source": "manual"},
    {"company_canonical_name": "Toyota Motor Corporation", "facility_type": "pack_plant", "country": "US", "region": "Kentucky", "city": "Georgetown", "status": "operating", "capacity_notes": "Largest Toyota plant in US. Adding BEV production lines 2025–2026.", "latitude": 38.2098, "longitude": -84.5573, "data_source": "manual"},
    {"company_canonical_name": "Toyota Motor Corporation", "facility_type": "cell_factory", "country": "US", "region": "North Carolina", "city": "Liberty", "status": "under_construction", "capacity_notes": "Toyota Battery Manufacturing NC; ~40 GWh/yr planned. LFP and NMC prismatic cells for US BEV production.", "latitude": 35.8529, "longitude": -79.5729, "data_source": "manual"},

    # Honda Motor Company
    {"company_canonical_name": "Honda Motor Company", "facility_type": "pack_plant", "country": "US", "region": "Ohio", "city": "Marysville", "status": "operating", "capacity_notes": "Primary US plant; transitioning to EV production mid-decade.", "latitude": 40.2362, "longitude": -83.3669, "data_source": "manual"},
    {"company_canonical_name": "Honda Motor Company", "facility_type": "pack_plant", "country": "US", "region": "Ohio", "city": "Jeffersonville", "status": "under_construction", "capacity_notes": "New EV-dedicated plant, part of Honda EV Hub Ohio. ~240,000 vehicles/yr.", "data_source": "manual"},

    # Nissan Motor Company
    {"company_canonical_name": "Nissan Motor Company", "facility_type": "pack_plant", "country": "GB", "region": "Tyne and Wear", "city": "Sunderland", "status": "operating", "capacity_notes": "Leaf, Ariya production. Envision AESC cell supply from adjacent plant.", "latitude": 54.9085, "longitude": -1.3810, "data_source": "manual"},
    {"company_canonical_name": "Nissan Motor Company", "facility_type": "pack_plant", "country": "US", "region": "Tennessee", "city": "Smyrna", "status": "operating", "capacity_notes": "Leaf US production. Ariya planned.", "latitude": 35.9829, "longitude": -86.5186, "data_source": "manual"},
    {"company_canonical_name": "Nissan Motor Company", "facility_type": "pack_plant", "country": "JP", "region": "Kanagawa", "city": "Oppama", "status": "operating", "capacity_notes": "Leaf, Ariya JP production", "data_source": "manual"},

    # Subaru Corporation
    {"company_canonical_name": "Subaru Corporation", "facility_type": "pack_plant", "country": "JP", "region": "Gunma", "city": "Ota", "status": "operating", "capacity_notes": "Solterra BEV (co-developed with Toyota). Crosstrek PHEV.", "data_source": "manual"},
    {"company_canonical_name": "Subaru Corporation", "facility_type": "pack_plant", "country": "US", "region": "Indiana", "city": "Lafayette", "status": "operating", "capacity_notes": "Outback, Legacy, Ascent. PHEV variants planned. Samsung SDI cells.", "latitude": 40.4259, "longitude": -86.8957, "data_source": "manual"},

    # Rivian Automotive
    {"company_canonical_name": "Rivian Automotive", "facility_type": "pack_plant", "country": "US", "region": "Illinois", "city": "Normal", "status": "operating", "capacity_notes": "R1T, R1S, EDV delivery vans. ~150,000 vehicles/yr capacity. Samsung SDI cells.", "latitude": 40.5142, "longitude": -88.9906, "data_source": "manual"},
    {"company_canonical_name": "Rivian Automotive", "facility_type": "pack_plant", "country": "US", "region": "Georgia", "city": "Social Circle", "status": "planned", "capacity_notes": "R2 platform facility; ~400,000 vehicles/yr planned. Construction paused 2024 pending financing.", "latitude": 33.6576, "longitude": -83.7185, "data_source": "manual"},

    # Lucid Group
    {"company_canonical_name": "Lucid Group", "facility_type": "pack_plant", "country": "US", "region": "Arizona", "city": "Casa Grande", "status": "operating", "capacity_notes": "AMP-1; Lucid Air. ~34,000 vehicles/yr capacity; utilisation well below capacity.", "latitude": 32.8795, "longitude": -111.7574, "data_source": "manual"},
    {"company_canonical_name": "Lucid Group", "facility_type": "pack_plant", "country": "SA", "region": "Makkah", "city": "King Abdullah Economic City", "status": "under_construction", "capacity_notes": "AMP-2; Gravity SUV for Middle East market. Saudi PIF funded.", "data_source": "manual"},

    # Stellantis
    {"company_canonical_name": "Stellantis", "facility_type": "pack_plant", "country": "IT", "region": "Piedmont", "city": "Turin", "status": "operating", "capacity_notes": "Mirafiori; Fiat 500e. CATL cells.", "latitude": 45.0375, "longitude": 7.5797, "data_source": "manual"},
    {"company_canonical_name": "Stellantis", "facility_type": "pack_plant", "country": "CA", "region": "Ontario", "city": "Windsor", "status": "operating", "capacity_notes": "Chrysler Pacifica PHEV. Samsung SDI cells.", "latitude": 42.3149, "longitude": -83.0364, "data_source": "manual"},
    {"company_canonical_name": "Stellantis", "facility_type": "pack_plant", "country": "FR", "region": "Hauts-de-France", "city": "Douvrin", "status": "operating", "capacity_notes": "ACC JV (TotalEnergies/Saft/Stellantis); 13 GWh/yr initial. Peugeot, Citroën, Opel EV supply.", "data_source": "manual"},

    # Mercedes-Benz Group
    {"company_canonical_name": "Mercedes-Benz Group", "facility_type": "pack_plant", "country": "DE", "region": "Baden-Württemberg", "city": "Sindelfingen", "status": "operating", "capacity_notes": "EQS, EQE sedan. Factory 56. CATL cells.", "latitude": 48.6940, "longitude": 9.0057, "data_source": "manual"},
    {"company_canonical_name": "Mercedes-Benz Group", "facility_type": "pack_plant", "country": "US", "region": "Alabama", "city": "Tuscaloosa", "status": "operating", "capacity_notes": "EQS SUV, EQE SUV. CATL cells (US-made at CATL-Ford facility via license).", "latitude": 33.2148, "longitude": -87.5692, "data_source": "manual"},
    {"company_canonical_name": "Mercedes-Benz Group", "facility_type": "pack_plant", "country": "CN", "region": "Beijing", "city": "Beijing", "status": "operating", "capacity_notes": "BBAC JV; EQE, EQC for China market.", "data_source": "manual"},

    # Volvo Car Group
    {"company_canonical_name": "Volvo Car Group", "facility_type": "pack_plant", "country": "BE", "region": "East Flanders", "city": "Ghent", "status": "operating", "capacity_notes": "EX30, EX40, C40. Largest Volvo plant. CATL cells.", "latitude": 51.0775, "longitude": 3.6919, "data_source": "manual"},
    {"company_canonical_name": "Volvo Car Group", "facility_type": "pack_plant", "country": "SE", "region": "Västra Götaland", "city": "Gothenburg", "status": "operating", "capacity_notes": "EX90, XC40 Recharge. HQ and primary Sweden plant.", "latitude": 57.7089, "longitude": 11.9746, "data_source": "manual"},
    {"company_canonical_name": "Volvo Car Group", "facility_type": "pack_plant", "country": "US", "region": "South Carolina", "city": "Berkeley County", "status": "under_construction", "capacity_notes": "EX90 US production; ~150,000 vehicles/yr planned. IRA-qualifying.", "data_source": "manual"},
    {"company_canonical_name": "Volvo Car Group", "facility_type": "pack_plant", "country": "CN", "region": "Sichuan", "city": "Chengdu", "status": "operating", "capacity_notes": "EX40, S60 for China market. CATL cells.", "data_source": "manual"},

    # Polestar Automotive
    {"company_canonical_name": "Polestar Automotive", "facility_type": "pack_plant", "country": "CN", "region": "Sichuan", "city": "Chengdu", "status": "operating", "capacity_notes": "Polestar 2, 4. Primary manufacturing. CATL cells.", "latitude": 30.5728, "longitude": 104.0668, "data_source": "manual"},
    {"company_canonical_name": "Polestar Automotive", "facility_type": "pack_plant", "country": "KR", "region": "Busan", "city": "Busan", "status": "operating", "capacity_notes": "Polestar 3, 5 via Samsung (Renault Samsung plant). Samsung SDI cells.", "data_source": "manual"},

    # Geely Auto Group
    {"company_canonical_name": "Geely Auto Group", "facility_type": "pack_plant", "country": "CN", "region": "Zhejiang", "city": "Hangzhou", "status": "operating", "capacity_notes": "Primary Geely EV assembly. Lynk & Co, Geometry brands. CATL and CALB cells.", "data_source": "manual"},

    # ── RECYCLER FACILITIES ───────────────────────────────────────────────
    # NOTE: Mine and refinery records have been removed from this seed file.
    # They are now populated by: bdi-ingest ingest-gem
    # GEM provides quarterly-updated capacity data that feeds the operational
    # scoring pillar. Adding mines/refineries here would create duplicates.

    {"company_canonical_name": "Redwood Materials", "facility_type": "recycling", "country": "US", "region": "Nevada", "city": "McCarran", "status": "operating", "capacity_notes": "Primary campus; battery recycling and cathode/anode material production. ~100 GWh/yr recycling capacity target. Ford, Panasonic, Amazon as input partners.", "latitude": 39.6101, "longitude": -119.4822, "data_source": "manual"},
    {"company_canonical_name": "Redwood Materials", "facility_type": "recycling", "country": "US", "region": "South Carolina", "city": "Charleston", "status": "under_construction", "capacity_notes": "Battery materials campus; anode copper foil production. ~100 GWh/yr planned 2025. IRA-qualifying domestic content.", "latitude": 32.7765, "longitude": -79.9311, "data_source": "manual"},
    {"company_canonical_name": "Cirba Solutions", "facility_type": "recycling", "country": "US", "region": "Ohio", "city": "Lancaster", "status": "operating", "capacity_notes": "Largest US battery recycling facility by volume. Li-ion, NiMH, lead-acid. Black mass processing.", "latitude": 39.7137, "longitude": -82.5996, "data_source": "manual"},
    {"company_canonical_name": "Cirba Solutions", "facility_type": "recycling", "country": "US", "region": "South Carolina", "city": "Ellsworth", "status": "operating", "capacity_notes": "Hazardous battery processing and black mass recovery.", "data_source": "manual"},
]

# ---------------------------------------------------------------------------
# Seed function
# ---------------------------------------------------------------------------

_ALLOWED_FACILITY_TYPES = {
    "mine", "refinery", "cell_factory", "pack_plant", "recycling", "r_and_d", "hq",
}
_ALLOWED_STATUSES = {
    "operating", "planned", "under_construction", "mothballed", "closed",
}


def seed_facilities(session: Session) -> dict[str, int]:
    """Upsert all curated facilities. Idempotent.

    Two-phase dedup:
      1. Find or create a ``Facility`` row by (facility_type, country, city).
         City comparison is NULL-safe.
      2. Find or create a ``CompanyFacility`` junction row by (company_id,
         facility_id). If the link already exists, skip it; if the facility
         record changed, update its mutable fields.

    This supports the many-to-many design: a JV facility is one ``Facility``
    row linked to multiple companies via separate ``CompanyFacility`` rows.

    Returns {"inserted": int, "updated": int, "skipped": int, "companies_not_found": int}
    """
    inserted = 0
    updated = 0
    skipped = 0
    companies_not_found = 0

    # Cache company lookups to avoid repeated DB queries for the same name.
    company_cache: dict[str, object] = {}

    for entry in _FACILITIES:
        canonical_name: str = entry["company_canonical_name"]

        # Resolve company — cached after first lookup.
        if canonical_name not in company_cache:
            company_cache[canonical_name] = session.scalar(
                select(Company).where(Company.canonical_name == canonical_name)
            )

        company = company_cache[canonical_name]
        if company is None:
            log.warning(
                "seed_facilities.company_not_found",
                canonical_name=canonical_name,
            )
            companies_not_found += 1
            continue

        facility_type: str = entry["facility_type"]
        country: str = entry["country"]
        city = entry.get("city")
        ownership_type: str = entry.get("ownership_type", "operator")

        # ── Phase 1: find or create the Facility record ───────────────────────
        # NULL-safe city comparison: treat two NULL cities as the same facility.
        city_cond = (
            Facility.city.is_(None) if city is None else Facility.city == city
        )

        existing_facility = session.scalar(
            select(Facility).where(
                and_(
                    Facility.facility_type == facility_type,
                    Facility.country == country,
                    city_cond,
                )
            )
        )

        seed_values = {
            "status": entry.get("status", "operating"),
            "capacity_notes": entry.get("capacity_notes"),
            "latitude": entry.get("latitude"),
            "longitude": entry.get("longitude"),
            "data_source": entry.get("data_source", "manual"),
        }

        if existing_facility is None:
            existing_facility = Facility(
                facility_type=facility_type,
                country=country,
                region=entry.get("region"),
                city=city,
                **seed_values,
            )
            session.add(existing_facility)
            session.flush()  # assign id before creating junction row
            log.info(
                "seed_facilities.facility_inserted",
                facility_type=facility_type,
                country=country,
                city=city,
            )
        else:
            # Update mutable fields if the seed data changed.
            changed = [
                f for f, v in seed_values.items()
                if getattr(existing_facility, f) != v
            ]
            for f in changed:
                setattr(existing_facility, f, seed_values[f])
            if changed:
                log.info(
                    "seed_facilities.facility_updated",
                    facility_type=facility_type,
                    country=country,
                    city=city,
                    changed_fields=changed,
                )

        # ── Phase 2: find or create the CompanyFacility junction row ──────────
        existing_link = session.scalar(
            select(CompanyFacility).where(
                and_(
                    CompanyFacility.company_id == company.id,
                    CompanyFacility.facility_id == existing_facility.id,
                )
            )
        )

        if existing_link is not None:
            log.debug(
                "seed_facilities.link_exists",
                canonical_name=canonical_name,
                facility_type=facility_type,
                country=country,
                city=city,
            )
            skipped += 1
            continue

        link = CompanyFacility(
            company_id=company.id,
            facility_id=existing_facility.id,
            ownership_type=ownership_type,
            ownership_pct=entry.get("ownership_pct"),
        )
        session.add(link)
        log.info(
            "seed_facilities.link_inserted",
            canonical_name=canonical_name,
            facility_type=facility_type,
            country=country,
            city=city,
            ownership_type=ownership_type,
        )
        inserted += 1

    session.commit()

    log.info(
        "seed_facilities.done",
        inserted=inserted,
        updated=updated,
        skipped=skipped,
        companies_not_found=companies_not_found,
    )
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "companies_not_found": companies_not_found,
    }
