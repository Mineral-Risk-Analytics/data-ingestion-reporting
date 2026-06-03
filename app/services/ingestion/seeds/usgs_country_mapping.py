"""USGS MCS country name → ISO-2 lookup.

Extracted from ``usgs_mcs_parser.py`` (the deprecated 2025 wide-format
parser) on 2026-05-24 so the 2026 parser doesn&#x27;t have a load-time
dependency on a module that is being deleted.  Behaviour is unchanged
from the original implementation.

How resolution works
--------------------
The dict is built by walking ``seed_countries._COUNTRIES`` once at module
load and indexing both the canonical ``name`` field and every entry in
``common_names`` against the row&#x27;s ISO-2.  That way MCS country names
that already exist in the broader seed (e.g. "Russia" → "RU") resolve
through the same mapping table that GTA / Comtrade / GeographyCache use.

``_COUNTRY_ISO2_OVERRIDES`` adds USGS-specific name variants that
*do not belong in seed_countries.common_names* — adding them there
would create false positives in free-text geography detection (e.g.
"Burma" matching an article about Burma Shave razors).  Keep this
override set minimal.

To add support for a new country: edit ``seed_countries._COUNTRIES``
and re-run ``bdi-ingest seed-countries``.  The lookup picks up the new
names on next import.  Use ``_COUNTRY_ISO2_OVERRIDES`` only when MCS
uses a name that doesn&#x27;t belong in the broader seed.

Module imports avoid load-time circularity by deferring the
``seed_countries`` import into ``_build_country_lookup``.
"""

from __future__ import annotations


# MCS-specific name variants that don&#x27;t belong in seed_countries.common_names.
# Kept here because the seed&#x27;s ``common_names`` is read by GTA / GeographyCache
# / Comtrade for free-text and trade-flow attribution; adding USGS-style names
# there would introduce false positives in those code paths.
_COUNTRY_ISO2_OVERRIDES: dict[str, str] = {
    # USGS uses "Korea, Republic of" / "Korea, North"; other ingesters use
    # "South Korea" / "North Korea".  Seed common_names carries the latter;
    # this dict adds the USGS form.
    "Korea, Republic of": "KR",
    "Korea, North":       "KP",
    "Czech Republic":     "CZ",   # USGS form; seed uses "Czechia"
    "Burma":              "MM",   # USGS uses "Burma"; seed uses "Myanmar"
    "Côte d’Ivoire": "CI",   # U+2019 right single quote — verbatim from MCS file
    "Côte d'Ivoire":      "CI",   # ASCII apostrophe fallback
}


def _build_country_lookup() -> dict[str, str]:
    """Walk ``seed_countries._COUNTRIES`` and build a name → ISO-2 lookup.

    Indexes both the canonical ``name`` and every entry in ``common_names``
    so MCS country names resolve via the same dict that GTA / Comtrade /
    etc. use.

    Imported lazily inside the function to avoid a circular import at
    module load time.
    """
    from app.services.ingestion.seed_countries import _COUNTRIES  # local import

    lookup: dict[str, str] = {}
    for entry in _COUNTRIES:
        iso2 = entry["iso2"]
        if entry.get("name"):
            lookup[entry["name"]] = iso2
        for alt in entry.get("common_names") or []:
            lookup[alt] = iso2
    # MCS-specific overrides win over seed values if they conflict.  In
    # practice they shouldn&#x27;t — overrides are explicitly USGS-only forms.
    lookup.update(_COUNTRY_ISO2_OVERRIDES)
    return lookup


# Resolved once at module load.  Tests that need to override behaviour
# (e.g. minimal fixtures without seed_countries) can monkey-patch this dict.
_COUNTRY_ISO2: dict[str, str] = _build_country_lookup()


# Aggregate / non-country row labels to exclude from per-country rankings.
# Lower-cased for case-insensitive matching at the call site.
# 2026 parser uses inline ``"world" in name.lower()`` which catches
# the "world total" variants but misses "other countries" and the
# regional aggregates here — call sites that need full coverage should
# use this set instead.
_EXCLUDE_COUNTRIES: frozenset[str] = frozenset({
    "world total (rounded)",
    "world total",
    "other countries",
    "united states and canada",
})


__all__ = [
    "_COUNTRY_ISO2",
    "_COUNTRY_ISO2_OVERRIDES",
    "_EXCLUDE_COUNTRIES",
    "_build_country_lookup",
]
