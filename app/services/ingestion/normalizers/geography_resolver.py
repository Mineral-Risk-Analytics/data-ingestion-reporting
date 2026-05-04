"""
Normalize geography fields (Census CTY codes, loose strings) toward stable ISO2 + region labels.

Phase 1 uses a compact static map for CTY code lookups.
Phase 2 adds GeographyCache — a DB-backed class that loads country
``detection_patterns`` from the ``countries`` table and replaces the
hardcoded ``_GEO_PATTERNS`` list in ``ingest_federal_register.py``.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

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


# ---------------------------------------------------------------------------
# GeographyCache — DB-backed country pattern detection
# ---------------------------------------------------------------------------

_GEO_RELEVANCE: dict[str, float] = {"primary": 0.9, "mentioned": 0.6}


class GeographyCache:
    """
    Pre-built lookup of country text patterns → (iso2, context, relevance_score).
    Built once per ingest run from ``countries.detection_patterns`` (JSONB) and
    reused across events.

    Replaces the hardcoded ``_GEO_PATTERNS`` list in ``ingest_federal_register.py``
    and ``_detect_geographies()`` helper.

    Pattern structure in DB:
        [{"pattern": "xinjiang", "context": "primary"},
         {"pattern": "chinese",  "context": "mentioned"}, ...]

    context "primary"  → country is directly referenced  (relevance 0.9)
    context "mentioned" → adjectival/contextual reference (relevance 0.6)

    Patterns are ordered longest-first so that "democratic republic of congo"
    matches before a bare "congo" fallback within the same iteration.
    """

    def __init__(
        self,
        entries: list[tuple[str, str, str, float]],
    ) -> None:
        # entries: [(pattern_lower, iso2, context, relevance)]
        # Sorted longest-first so more-specific patterns win.
        self._entries = sorted(entries, key=lambda e: len(e[0]), reverse=True)

    @classmethod
    def build(cls, session: Session) -> "GeographyCache":
        """
        Load all countries that have non-null ``detection_patterns`` and build
        the pattern → (iso2, context, relevance) lookup.
        """
        from app.models.country import Country  # avoid circular at module level

        rows = session.execute(
            select(Country.iso2, Country.detection_patterns).where(
                Country.detection_patterns.isnot(None)
            )
        ).all()

        entries: list[tuple[str, str, str, float]] = []
        for iso2, patterns in rows:
            if not patterns or not isinstance(patterns, list):
                continue
            for pat in patterns:
                if not isinstance(pat, dict):
                    continue
                pattern = pat.get("pattern", "")
                context = pat.get("context", "mentioned")
                if not pattern:
                    continue
                relevance = _GEO_RELEVANCE.get(context, 0.6)
                entries.append((pattern.lower(), iso2, context, relevance))

        return cls(entries)

    def detect(self, text: str) -> list[tuple[str, str, float]]:
        """
        Scan ``text`` for country references.

        Returns ``[(iso2, geography_context, relevance_score)]``, deduplicated
        by ISO2 — if a country matches both a primary and a mentioned pattern,
        primary wins (higher relevance_score takes precedence).
        """
        lower = text.lower()
        seen: dict[str, tuple[str, float]] = {}  # iso2 → (context, relevance)

        for pattern, iso2, context, relevance in self._entries:
            if pattern not in lower:
                continue
            existing = seen.get(iso2)
            if existing is None or relevance > existing[1]:
                seen[iso2] = (context, relevance)

        return [(iso2, ctx, score) for iso2, (ctx, score) in seen.items()]
