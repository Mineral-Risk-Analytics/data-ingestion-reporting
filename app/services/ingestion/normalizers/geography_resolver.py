"""
Normalize geography fields (Census CTY codes, loose strings) toward stable ISO2 + region labels.

Phase 1 uses a compact static map; Phase 3+ may load from `countries` table.
"""

from __future__ import annotations

# Common Census `CTY_CODE` → ISO2 (subset for batteries / trade demos)
_CTY_TO_ISO2: dict[str, str] = {
    "5700": "CN",
    "5830": "KR",
    "5880": "JP",
    "1220": "CA",
    "2010": "MX",
    "4270": "DE",
    "4330": "FR",
    "4120": "NL",
    "0000": "US",  # total / world aggregate sometimes
    "0015": "WOR",  # placeholder non-ISO for "World"
}


class GeographyResolver:
    def partner_country_iso2(self, census_cty_code: str | None) -> str | None:
        if not census_cty_code:
            return None
        code = str(census_cty_code).strip()
        return _CTY_TO_ISO2.get(code, code if len(code) == 2 else code)

    def region_for_country(self, iso2: str | None) -> str | None:
        if not iso2:
            return None
        asia = {"CN", "JP", "KR", "TW", "VN", "ID", "MY", "TH", "PH"}
        europe = {"DE", "FR", "NL", "PL", "HU", "SE", "NO", "FI", "ES", "IT"}
        na = {"US", "CA", "MX"}
        if iso2 in na:
            return "north_america"
        if iso2 in asia:
            return "asia_pacific"
        if iso2 in europe:
            return "europe"
        return "other"
