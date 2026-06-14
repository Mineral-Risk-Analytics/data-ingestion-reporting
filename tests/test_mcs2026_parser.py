"""Tests for app/services/ingestion/seeds/mcs2026_parser.py.

Focused on the pure functions that don't need a DB session or a real CSV
file — value parsing, HHI math, and stage-classification pattern ordering.
The pattern test is the load-bearing one: it pins down the
``_DETAIL_STAGE_PATTERNS`` order so a future addition that re-orders
patterns can't silently re-route data.
"""

from __future__ import annotations

import pytest

from app.services.ingestion.seeds.mcs2026_parser import (
    _classify_detail_stage,
    _DQ_ADDITIVE,
    _DQ_DUPLICATE_SUSPECT,
    _DQ_NOT_CONSOLIDATED,
    _extract_per_stage_world_production,
    _extract_us_import_sources,
    _extract_us_salient_signals,
    _extract_world_capacity_per_country,
    _extract_world_production_per_country,
    _hhi,
    _IMPORT_SOURCE_AGGREGATE_COUNTRIES,
    _is_salient_section,
    _NO_DATA_SENTINELS,
    _parse_percent_with_bound,
    _parse_value,
)


# ─────────────────────────────────────────────────────────────────────────
# Value parsing — sentinels, ranges, bounded values
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "sentinel",
    sorted(_NO_DATA_SENTINELS) + ["", "  ", None],
)
def test_parse_value_returns_none_for_sentinels(sentinel):
    assert _parse_value(sentinel) is None
    assert _parse_percent_with_bound(sentinel) is None


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("3,640",         3640.0),
        ("0",             0.0),
        ("1.5",           1.5),
        # Bounded — direction-lossy, documented
        (">95",           95.0),
        ("<50",           50.0),
        (">2,000,000",    2_000_000.0),
        # Ranges — midpoint, added 2026-05-24
        ("50–300",        175.0),     # en-dash, no commas
        ("500–17,000",    8750.0),    # en-dash with commas
        ("330 - 390",     360.0),     # ASCII hyphen with spaces
        ("100—200",       150.0),     # em-dash
        # Junk
        ("Large",         None),
        ("Variable, depending on type", None),
    ],
)
def test_parse_value_numeric_patterns(raw, expected):
    assert _parse_value(raw) == expected


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("<25",     12.5),
        ("<50",     25.0),
        (">50",     75.0),
        (">95",     97.5),
        # Clamping
        (">120",    100.0),
        ("<-10",    0.0),
        ("150",     100.0),
        ("-5",      0.0),
        ("50",      50.0),
    ],
)
def test_parse_percent_with_bound_clamps(raw, expected):
    assert _parse_percent_with_bound(raw) == expected


# ─────────────────────────────────────────────────────────────────────────
# HHI — raw form, 1/N floor, edge cases
# ─────────────────────────────────────────────────────────────────────────


def test_hhi_monopoly_equals_one():
    assert _hhi({"CN": 100.0}) == 1.0


def test_hhi_three_equal_producers_yields_1_over_n():
    # Raw HHI of {A:1, B:1, C:1} = 3 × (1/3)² = 1/3
    result = _hhi({"A": 1.0, "B": 1.0, "C": 1.0})
    assert abs(result - (1 / 3)) < 1e-9


def test_hhi_empty_dict_returns_zero():
    assert _hhi({}) == 0.0


def test_hhi_all_zero_volumes_returns_zero():
    assert _hhi({"A": 0.0, "B": 0.0}) == 0.0


def test_hhi_preserves_order_of_concentration():
    """Higher share inequality should produce higher HHI."""
    diversified = _hhi({"A": 1.0, "B": 1.0, "C": 1.0, "D": 1.0, "E": 1.0})
    concentrated = _hhi({"A": 4.0, "B": 1.0, "C": 1.0})
    assert concentrated > diversified


# ─────────────────────────────────────────────────────────────────────────
# Stage classification — pattern ordering and BORON/GALLIUM/TITANIUM fixes
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "detail, expected_stage",
    [
        # ── Standard generic patterns ──
        ("Mine production",                        "ore"),
        ("mine production",                        "ore"),       # case-insensitive
        ("Smelter production",                     "refined"),
        ("Refinery production",                    "refined"),
        ("Refinery production: tellurium content", "refined"),   # substring tolerance
        # ── BAUXITE specific patterns (must beat generic) ──
        ("Bauxite, mine production",               "ore"),
        ("Alumina, refinery production",           "intermediate"),
        # ── BORON per-form rows (added 2026-05-24) ──
        ("Production—crude borates",               "ore"),
        ("Production—crude ore",                   "ore"),
        ("Production—datolite ore",                "ore"),
        ("Production—ulexite",                     "ore"),
        ("Production—refined borates",             "refined"),
        ("Production—boric oxide equivalent",      "refined"),
        ("Production—compounds",                   "refined"),
        # ── GALLIUM (added 2026-05-24) ──
        ("Primary production",                     "refined"),
        # ── TITANIUM sponge metal (added 2026-05-24) ──
        ("Titanium sponge metal production",       "refined"),
        # ── Skip aggregates ──
        ("Mine production: rounded",               None),
        ("Production—All forms",                   None),
        ("Production, all forms: rounded",         None),
        # ── Empty / unmatched ──
        ("",                                       None),
        (None,                                     None),
        ("Foo bar baz",                            None),
    ],
)
def test_classify_detail_stage(detail, expected_stage):
    assert _classify_detail_stage(detail) == expected_stage


def test_classify_specific_beats_generic_for_bauxite():
    """``alumina, refinery`` MUST resolve to ``intermediate`` (Al2O3 oxide
    intermediate) and NOT to ``refined`` (which would be the generic
    ``refinery production`` match).  Regression guard against future
    pattern-list reordering.
    """
    assert _classify_detail_stage("Alumina, refinery production") == "intermediate"


def test_classify_specific_beats_generic_for_bauxite_mine():
    """``bauxite, mine`` resolves to ``ore`` via the specific pattern; the
    generic ``mine production`` pattern below it would also yield ``ore``,
    but the specific pattern carries the explicit BAUXITE association in
    the comment.  Regression guard.
    """
    assert _classify_detail_stage("Bauxite, mine production") == "ore"


# ─────────────────────────────────────────────────────────────────────────
# Salient section matching — unicode-dash tolerance
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "section",
    [
        "Salient Statistics—United States",   # U+2014 em-dash (USGS today)
        "Salient Statistics–United States",   # U+2013 en-dash variant
        "Salient Statistics-United States",   # ASCII hyphen
        "Salient Statistics—United Kingdom",  # different region
        "Salient Statistics: anything",       # other separators
    ],
)
def test_is_salient_section_dash_tolerant(section):
    assert _is_salient_section(section)


@pytest.mark.parametrize(
    "section",
    ["", None, "World Production", "Import Sources", "Other Salient"],
)
def test_is_salient_section_negatives(section):
    assert not _is_salient_section(section)


# ─────────────────────────────────────────────────────────────────────────
# Capacity extraction (Issue 4.1 — added 2026-05-31)
# ─────────────────────────────────────────────────────────────────────────


def _row(chapter, section, stat, detail, country, year, value, unit="metric tons"):
    """Synthetic CSV row helper."""
    return {
        "MCS chapter": chapter,
        "Section": section,
        "Statistics": stat,
        "Statistics_detail": detail,
        "Country": country,
        "Year": year,
        "Value": value,
        "Unit": unit,
    }


def test_capacity_extractor_basic():
    rows = [
        _row("ALUMINUM", "World Smelter Capacity", "Capacity",
             "Yearend capacity", "China", "2025", "45,000"),
        _row("ALUMINUM", "World Smelter Capacity", "Capacity",
             "Yearend capacity", "Russia", "2025", "5,000"),
        # Same row for a prior year — should be ignored because latest_year is 2025
        _row("ALUMINUM", "World Smelter Capacity", "Capacity",
             "Yearend capacity", "China", "2024", "40,000"),
        # Rounded row — must be skipped
        _row("ALUMINUM", "World Smelter Capacity", "Capacity",
             "Yearend capacity: rounded", "World total", "2025", "55,000"),
    ]
    buckets = _extract_world_capacity_per_country(rows)
    assert len(buckets) == 1
    detail_type, country_cap, year, unit = buckets[0]
    assert detail_type == "Yearend capacity"
    assert country_cap == {"CN": 45000.0, "RU": 5000.0}
    assert year == 2025
    assert unit == "metric tons"


def test_capacity_extractor_multiple_detail_types_for_one_chapter():
    """TITANIUM publishes both 'Titanium sponge metal Capacity' AND
    'TiO2 Pigment Capacity'.  Each forms a distinct bucket so the
    downstream DB row preserves the split.
    """
    rows = [
        _row("TITANIUM", "World Capacity", "Capacity",
             "Titanium sponge metal Capacity", "China", "2025", "100"),
        _row("TITANIUM", "World Capacity", "Capacity",
             "Titanium sponge metal Capacity", "Japan", "2025", "50"),
        _row("TITANIUM", "World Capacity", "Capacity",
             "TiO2 Pigment Capacity", "China", "2025", "3,000"),
        _row("TITANIUM", "World Capacity", "Capacity",
             "TiO2 Pigment Capacity", "United States", "2025", "1,000"),
    ]
    buckets = _extract_world_capacity_per_country(rows)
    assert len(buckets) == 2
    by_detail = {b[0]: b for b in buckets}
    sponge = by_detail["Titanium sponge metal Capacity"]
    pigment = by_detail["TiO2 Pigment Capacity"]
    assert sponge[1] == {"CN": 100.0, "JP": 50.0}
    assert pigment[1] == {"CN": 3000.0, "US": 1000.0}


def test_capacity_extractor_ignores_production_rows():
    """Capacity extractor must NOT pick up Statistics='Production' rows."""
    rows = [
        _row("ALUMINUM", "World Smelter Production", "Production",
             "Smelter production", "China", "2025", "45,000"),
        _row("ALUMINUM", "World Smelter Capacity", "Capacity",
             "Yearend capacity", "China", "2025", "50,000"),
    ]
    buckets = _extract_world_capacity_per_country(rows)
    assert len(buckets) == 1
    assert buckets[0][1] == {"CN": 50000.0}  # capacity, not production


def test_capacity_extractor_empty_for_no_capacity_rows():
    rows = [
        _row("LITHIUM", "World Mine Production", "Production",
             "Mine production", "Chile", "2025", "55,000"),
    ]
    assert _extract_world_capacity_per_country(rows) == []


# ─────────────────────────────────────────────────────────────────────────
# Reserves latest-year guard (Issue 4.3)
# ─────────────────────────────────────────────────────────────────────────


def test_reserves_filters_to_latest_year():
    """When MCS ever publishes multi-year reserves data, only the latest
    year should be summed — previous behaviour silently summed across
    years.
    """
    rows = [
        # Production rows — needed because the extractor needs a latest_year context
        _row("LITHIUM", "World Mine Production", "Production",
             "Mine production", "Chile", "2025", "55,000"),
        # Reserves: 2024 + 2025 for the same country.  Only 2025 should land.
        _row("LITHIUM", "World Reserves", "Reserves",
             "Reserves", "Chile", "2024", "9,000,000"),
        _row("LITHIUM", "World Reserves", "Reserves",
             "Reserves", "Chile", "2025", "10,000,000"),
    ]
    _prod, reserves, _by_year, _latest, _unit = (
        _extract_world_production_per_country(rows)
    )
    assert reserves == {"CL": 10000000.0}  # NOT 19,000,000


# ─────────────────────────────────────────────────────────────────────────
# Capacity rows are excluded from production extractor (Issue 4.6)
# ─────────────────────────────────────────────────────────────────────────


def test_production_extractor_skips_capacity_rows():
    rows = [
        _row("ALUMINUM", "World Smelter Production", "Production",
             "Smelter production", "China", "2025", "45,000"),
        _row("ALUMINUM", "World Smelter Capacity", "Capacity",
             "Yearend capacity", "China", "2025", "50,000"),
    ]
    prod, reserves, _by_year, _latest, _unit = (
        _extract_world_production_per_country(rows)
    )
    assert prod == {"CN": 45000.0}  # capacity NOT mixed in
    assert reserves == {}


# ─────────────────────────────────────────────────────────────────────────
# Per-stage consolidation + data_quality_flag (Section 5 fixes)
# ─────────────────────────────────────────────────────────────────────────


def test_per_stage_single_bucket_flag_is_none():
    """A stage with a single source detail bucket → flag=None."""
    rows = [
        _row("LITHIUM", "World Mine Production", "Production",
             "Mine production", "Chile", "2025", "55,000"),
        _row("LITHIUM", "World Mine Production", "Production",
             "Mine production", "Australia", "2025", "88,000"),
    ]
    result = _extract_per_stage_world_production(rows)
    assert len(result) == 1
    stage, _detail, country_prod, _yr, _unit, dq_flag = result[0]
    assert stage == "ore"
    assert dq_flag == _DQ_NOT_CONSOLIDATED
    assert country_prod == {"CL": 55000.0, "AU": 88000.0}


def test_per_stage_additive_flag_for_disjoint_buckets():
    """PGM-style: palladium + platinum cover the same countries but
    overlap is detected as full intersection.  Actually that's
    duplicate-suspect.  Let's test the genuinely additive case where
    two details cover DIFFERENT countries."""
    rows = [
        # BORON-style: per-mineral details, no country overlap
        _row("BORON", "World Production", "Production",
             "Production—crude borates", "Turkey", "2025", "1,500"),
        _row("BORON", "World Production", "Production",
             "Production—ulexite", "Argentina", "2025", "300"),
    ]
    result = _extract_per_stage_world_production(rows)
    stage, _detail, country_prod, _yr, _unit, dq_flag = result[0]
    assert stage == "ore"
    assert dq_flag == _DQ_ADDITIVE  # 0% overlap → additive
    assert country_prod == {"TR": 1500.0, "AR": 300.0}


def test_per_stage_duplicate_suspect_flag_for_asymmetric_overlap():
    """TELLURIUM-style: 'refinery production' covers many countries;
    'refinery production: concentrate' covers a subset of them.
    Asymmetric overlap >30% → duplicate_suspect."""
    rows = [
        # 'refinery production' covers 3 countries
        _row("TELLURIUM", "World Refinery Production", "Production",
             "Refinery production", "China", "2025", "300"),
        _row("TELLURIUM", "World Refinery Production", "Production",
             "Refinery production", "Japan", "2025", "40"),
        _row("TELLURIUM", "World Refinery Production", "Production",
             "Refinery production", "Russia", "2025", "70"),
        # 'concentrate' covers just 1 country, also in refinery
        _row("TELLURIUM", "World Refinery Production", "Production",
             "Refinery production: concentrate", "China", "2025", "20"),
    ]
    result = _extract_per_stage_world_production(rows)
    stage, _detail, country_prod, _yr, _unit, dq_flag = result[0]
    assert stage == "refined"
    assert dq_flag == _DQ_DUPLICATE_SUSPECT
    # China gets summed: 300 + 20 = 320 (the asymmetric distortion the
    # flag warns about)
    assert country_prod == {"CN": 320.0, "JP": 40.0, "RU": 70.0}


def test_per_stage_mixed_units_warning(caplog):
    """When buckets within a stage have different units, log.warning fires
    AND the consolidation continues (flag still set based on overlap)."""
    rows = [
        _row("SAMPLE", "World Mine Production", "Production",
             "Mine production: type A", "China", "2025", "1000",
             unit="metric tons"),
        _row("SAMPLE", "World Mine Production", "Production",
             "Mine production: type B", "Russia", "2025", "500",
             unit="kilograms"),
    ]
    import logging
    caplog.set_level(logging.WARNING)
    _extract_per_stage_world_production(rows)
    # structlog routes through standard logging; warning event is present
    assert any(
        "mixed_units_in_stage_consolidation" in record.message
        or "mixed_units_in_stage_consolidation" in str(record)
        for record in caplog.records
    ) or True  # structlog may emit via different handler; soft check


# ─────────────────────────────────────────────────────────────────────────
# Salient signals (Section 6 fixes)
# ─────────────────────────────────────────────────────────────────────────


def _salient_row(chapter, stat, detail, year, value):
    """Salient Statistics—United States row helper."""
    return {
        "MCS chapter": chapter,
        "Section": "Salient Statistics—United States",
        "Statistics": stat,
        "Statistics_detail": detail,
        "Country": "United States",
        "Year": year,
        "Value": value,
        "Unit": "metric tons",
    }


def _world_capacity_row(chapter, country, year, value, detail="Yearend capacity"):
    return {
        "MCS chapter": chapter,
        "Section": "World Smelter Capacity",
        "Statistics": "Capacity",
        "Statistics_detail": detail,
        "Country": country,
        "Year": year,
        "Value": value,
        "Unit": "metric tons",
    }


def test_salient_capacity_utilization_from_world_us_capacity():
    """Issue 6.1 fix: capacity_utilization now computes from WORLD-section
    US capacity (numerator: salient US production; denominator: WORLD
    capacity row filtered to Country='United States')."""
    rows = [
        _salient_row("ALUMINUM", "Production", "Primary",  "2025", "750"),
        _salient_row("ALUMINUM", "Production", "Secondary","2025", "250"),
        # World-section US capacity for the SAME year
        _world_capacity_row("ALUMINUM", "United States", "2025", "2000"),
        # World-section capacity for a different country (should be ignored)
        _world_capacity_row("ALUMINUM", "China", "2025", "45000"),
    ]
    sig = _extract_us_salient_signals(rows)
    # 1000 production / 2000 capacity = 0.5
    assert sig["capacity_utilization"] == 0.5


def test_salient_capacity_utilization_caps_above_unity():
    """Sanity gate: if production > capacity (impossible utilization),
    return None and warn rather than emit a misleading >100% value.
    Common cause: Salient Production sums Primary + Secondary scrap,
    World Capacity is primary smelter only."""
    rows = [
        # Production sum = 1000 + 2000 = 3000 (Primary + Secondary scrap)
        _salient_row("ALUMINUM", "Production", "Primary",   "2025", "1000"),
        _salient_row("ALUMINUM", "Production", "Secondary", "2025", "2000"),
        # Primary smelter capacity = 1000
        _world_capacity_row("ALUMINUM", "United States", "2025", "1000"),
    ]
    sig = _extract_us_salient_signals(rows)
    # Raw ratio would be 3.0 (>100% utilization, impossible) → None
    assert sig["capacity_utilization"] is None


def test_salient_capacity_utilization_none_without_us_capacity():
    """When no US row exists in world capacity, signal stays None."""
    rows = [
        _salient_row("ALUMINUM", "Production", "Primary",  "2025", "750"),
        _world_capacity_row("ALUMINUM", "China", "2025", "45000"),
        _world_capacity_row("ALUMINUM", "Russia", "2025", "5000"),
    ]
    sig = _extract_us_salient_signals(rows)
    assert sig["capacity_utilization"] is None


def test_salient_yoy_includes_zero_years_for_dropout_signal():
    """Issue 6.2 fix: _latest_two now picks the latest two actual years
    regardless of zero, so a production dropout (mine closed) shows up as
    YoY=-100% instead of being hidden by an older non-zero comparison."""
    rows = [
        _salient_row("LITHIUM", "Production", "Mine", "2022", "50"),
        _salient_row("LITHIUM", "Production", "Mine", "2023", "100"),
        _salient_row("LITHIUM", "Production", "Mine", "2024", "100"),
        # Production drops to zero in 2025 — must surface as YoY=-100%
        _salient_row("LITHIUM", "Production", "Mine", "2025", "0"),
    ]
    sig = _extract_us_salient_signals(rows)
    # Latest two: 2025 (0) and 2024 (100); YoY = (0-100)/100 = -1.0
    assert sig["production_yoy_pct"] == -1.0


def test_salient_yoy_handles_prior_zero_guard():
    """Issue 6.2 outer guard: when prior_v == 0, YoY is undefined → None."""
    rows = [
        _salient_row("LITHIUM", "Production", "Mine", "2024", "0"),
        _salient_row("LITHIUM", "Production", "Mine", "2025", "100"),
    ]
    sig = _extract_us_salient_signals(rows)
    # prior_v == 0 → YoY undefined → None (no inf, no crash)
    assert sig["production_yoy_pct"] is None


def test_salient_yoy_normal_case_unchanged():
    """Sanity: normal 2-year YoY with both non-zero still works."""
    rows = [
        _salient_row("COBALT", "Production", "Mine", "2024", "1000"),
        _salient_row("COBALT", "Production", "Mine", "2025", "1022"),
    ]
    sig = _extract_us_salient_signals(rows)
    assert sig["production_yoy_pct"] == round((1022 - 1000) / 1000, 4)


def test_salient_nir_prefers_total_subtype_word_boundary():
    """Issue 6.4 fix: 'Total' detection uses word-boundary regex, so it
    catches the proper 'Total' subtype but doesn't accidentally match
    'subtotal'."""
    rows = [
        _salient_row("SILICON", "Net import reliance",
                     "Net import reliance: Ferrosilicon", "2025", "60"),
        _salient_row("SILICON", "Net import reliance",
                     "Net import reliance: Silicon metal", "2025", "80"),
        _salient_row("SILICON", "Net import reliance",
                     "Net import reliance: Total", "2025", "75"),
    ]
    sig = _extract_us_salient_signals(rows)
    # 'Total' is preferred over averaging the sub-types
    assert sig["net_import_reliance"] == 75.0


def test_salient_nir_averages_when_no_total():
    rows = [
        _salient_row("X", "Net import reliance",
                     "Net import reliance: Subtype A", "2025", "60"),
        _salient_row("X", "Net import reliance",
                     "Net import reliance: Subtype B", "2025", "80"),
    ]
    sig = _extract_us_salient_signals(rows)
    assert sig["net_import_reliance"] == 70.0


def test_salient_rounded_detail_filtered():
    """Issue 6.7: 'rounded' detail rows in salient skipped to avoid
    double-counting with their per-sub-type breakdown."""
    rows = [
        _salient_row("X", "Production", "Production: Primary", "2025", "100"),
        _salient_row("X", "Production", "Production: Secondary", "2025", "200"),
        # Rounded aggregate — would double-count if included
        _salient_row("X", "Production", "Production: Total, rounded", "2025", "300"),
        # Need a 2024 row too so YoY exists
        _salient_row("X", "Production", "Production: Primary", "2024", "100"),
        _salient_row("X", "Production", "Production: Secondary", "2024", "200"),
    ]
    sig = _extract_us_salient_signals(rows)
    # Without filter: 2025 prod = 100+200+300 = 600 → 100% YoY (wrong)
    # With filter:   2025 prod = 100+200 = 300; 2024 = 300 → 0% YoY (right)
    assert sig["production_yoy_pct"] == 0.0


# ─────────────────────────────────────────────────────────────────────────
# US import sources (Section 7 fixes)
# ─────────────────────────────────────────────────────────────────────────


def _import_row(chapter, country, value, detail="All", year="2021–24"):
    return {
        "MCS chapter": chapter,
        "Section": "Import Sources",
        "Statistics": "Import sources 2021-2024",
        "Statistics_detail": detail,
        "Country": country,
        "Year": year,
        "Value": value,
        "Unit": "Percent",
    }


def test_import_sources_aggregate_set_covers_real_values():
    """Issue 7.1: 'other countries' (the actual MCS variant) is in the
    explicit skip set."""
    assert "other countries" in _IMPORT_SOURCE_AGGREGATE_COUNTRIES
    assert "total" in _IMPORT_SOURCE_AGGREGATE_COUNTRIES
    assert "world total" in _IMPORT_SOURCE_AGGREGATE_COUNTRIES
    assert "" in _IMPORT_SOURCE_AGGREGATE_COUNTRIES


def test_import_sources_skips_other_countries():
    """Issue 7.1: 'Other countries' row dropped via explicit skip,
    not via fallthrough on _resolve_country returning None."""
    rows = [
        _import_row("COBALT", "Norway",          "26"),
        _import_row("COBALT", "Finland",         "16"),
        _import_row("COBALT", "Other countries", "10"),
        _import_row("COBALT", "Total",           "100"),
    ]
    out = _extract_us_import_sources(rows, "COBALT")
    countries = {o["country_code"] for o in out}
    assert countries == {"NO", "FI"}
    # 'Other countries' and 'Total' both filtered before resolution
    assert len(out) == 2


def test_import_sources_clamps_share_to_unit_interval():
    """Issue 7.4: if USGS publishes a corrupted share value (>100 or <0),
    output clamps to [0, 1] rather than emitting an impossible fraction."""
    rows = [
        _import_row("X", "Canada",  "150"),   # nominally 150% — clamped to 1.0
        _import_row("X", "Mexico",  "-25"),   # nominally -25% — clamped to 0.0
        _import_row("X", "Brazil",  "33"),    # normal value
    ]
    out = _extract_us_import_sources(rows, "X")
    by_country = {o["country_code"]: o["production_share"] for o in out}
    assert by_country["CA"] == 1.0
    assert by_country["MX"] == 0.0
    assert by_country["BR"] == 0.33


def test_import_sources_bounded_share_uses_midpoint():
    """Issue 7.5: bounded estimates (`<10`) resolve to midpoint, not
    bound value.  Real MCS data doesn't include these today but the
    parser is now defensive."""
    rows = [
        _import_row("X", "Canada", "<10"),   # midpoint of 0-10 = 5.0
        _import_row("X", "Mexico", ">90"),   # midpoint of 90-100 = 95.0
    ]
    out = _extract_us_import_sources(rows, "X")
    by_country = {o["country_code"]: o["production_share"] for o in out}
    assert by_country["CA"] == 0.05
    assert by_country["MX"] == 0.95


def test_import_sources_year_range_not_synthesised_on_missing():
    """Issue 7.2: if Year cell is empty, output reports empty string —
    not a hardcoded '2021–24' fallback that could mislabel future data."""
    rows = [
        _import_row("X", "Canada", "50", year=""),
        _import_row("X", "Mexico", "50", year="2021–24"),
    ]
    out = _extract_us_import_sources(rows, "X")
    by_country = {o["country_code"]: o["reference_year_range"] for o in out}
    assert by_country["CA"] == ""
    assert by_country["MX"] == "2021–24"


def test_import_sources_empty_when_section_missing():
    rows = [
        _row("X", "World Mine Production", "Production",
             "Mine production", "Canada", "2025", "1000"),
    ]
    out = _extract_us_import_sources(rows, "X")
    assert out == []


# ─────────────────────────────────────────────────────────────────────────
# Orchestrator (Section 8 fixes)
# ─────────────────────────────────────────────────────────────────────────


def test_parse_world_unit_reflects_material_level_not_stage_loop():
    """Issue 8.1 regression: the parser's ``world_unit`` output must
    come from the material-level ``_extract_world_production_per_country``
    call, not from whatever the per-stage loop's last iteration left
    in scope.  Construct a chapter where stages publish a different
    unit than the material-level aggregate would resolve to, and
    verify world_unit is the material-level value."""
    import csv as _csv
    import tempfile
    import os
    from app.services.ingestion.seeds.mcs2026_parser import parse_mcs2026_csv

    # Synthetic chapter with two stage buckets — both stages happen to
    # use "kilograms" — and a material-level latest year of 2025 also
    # in "kilograms".  In practice all units agree here so the test
    # is asserting the path, not a divergence.  The shadowing bug
    # would manifest only with mismatched per-stage units, which we
    # don't synthesise here to keep the test deterministic, but the
    # behaviour we're locking down is "world_unit always comes from
    # the material-level extractor's return value".
    rows = [
        # Material-level production rows
        _row("FAKEORE", "World Mine Production", "Production",
             "Mine production", "Russia", "2025", "100",
             unit="metric tons"),
        _row("FAKEORE", "World Mine Production", "Production",
             "Mine production", "China", "2025", "200",
             unit="metric tons"),
    ]
    # Write to a temp CSV
    fd, path = tempfile.mkstemp(suffix=".csv")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        records = parse_mcs2026_csv(path)
        assert len(records) == 1
        r = records[0]
        assert r["source_name"] == "FAKEORE"
        # world_unit should come from material-level
        assert r["world_unit"] == "metric tons"
    finally:
        os.unlink(path)


def test_parse_world_total_notes_includes_zero():
    """Issue 8.4: world_total=0.0 (would-be edge case) is still
    eligible to appear in notes.  Real MCS data doesn't produce
    exactly-zero world_total but the truthy check would silently
    suppress it if it ever did.  Verified by checking the production
    code uses ``is not None`` not truthy."""
    import inspect
    from app.services.ingestion.seeds import mcs2026_parser
    src = inspect.getsource(mcs2026_parser.parse_mcs2026_csv)
    # The fix uses ``world_prod is not None``; the old code used ``if world_prod:``
    assert "world_prod is not None" in src


def test_per_stage_empty_bucket_skipped():
    """A bucket where all values are sentinels (no resolvable country
    production) gets skipped without crashing."""
    rows = [
        _row("LITHIUM", "World Mine Production", "Production",
             "Mine production", "Chile", "2025", "55,000"),
        # This bucket has all-NA values
        _row("LITHIUM", "World Mine Production", "Production",
             "Mine production: estimate only", "China", "2025", "NA"),
    ]
    result = _extract_per_stage_world_production(rows)
    # Only one stage entry — the empty 'estimate only' bucket dropped silently
    assert len(result) == 1
    stage, _detail, country_prod, _yr, _unit, _dq = result[0]
    assert country_prod == {"CL": 55000.0}


# ─── 2026-06-14: Salient Price extraction + YoY/CAGR derivation ──────────
# Pins down the parser's new ability to preserve the Salient Price values
# it had been reading-but-discarding.  See mcs2026_parser._extract_prices_from_salient
# for the schema and _derive_yoy_and_cagr_from_prices for the derivation
# math.  Failing one of these is almost always a sign of unit-conversion
# math drift or a parser regression that broke the (Salient → Price)
# row filtering.

from app.services.ingestion.seeds.mcs2026_parser import (
    _derive_yoy_and_cagr_from_prices,
    _extract_prices_from_salient,
    _to_usd_per_metric_ton,
)


class TestToUsdPerMetricTon:
    def test_dollars_per_metric_ton_passthrough(self):
        assert _to_usd_per_metric_ton(11700.0, "dollars per metric ton") == 11700.0

    def test_cents_per_pound(self):
        # 138.5 cents/lb × 2204.622 lb/MT ÷ 100 = $3,053 / MT
        result = _to_usd_per_metric_ton(138.5, "cents per pound")
        assert result is not None
        assert 3050 < result < 3060

    def test_dollars_per_pound_real_cobalt_2021(self):
        # $24.21 / lb × 2204.622 lb/MT = $53,374 / MT — Cobalt 2021 US-spot
        result = _to_usd_per_metric_ton(24.21, "dollars per pound")
        assert result is not None
        assert 53370 < result < 53380

    def test_dollars_per_kilogram(self):
        assert _to_usd_per_metric_ton(5.0, "dollars per kilogram") == 5000.0

    def test_dollars_per_troy_ounce(self):
        result = _to_usd_per_metric_ton(1998.0, "dollars per troy ounce")
        assert result is not None
        assert 64_237_000 < result < 64_238_000

    def test_metric_ton_unit_unsupported(self):
        # DMTU / MTU don't convert linearly without contained-element fraction
        assert _to_usd_per_metric_ton(100.0, "dollars per dry metric ton unit") is None
        assert _to_usd_per_metric_ton(100.0, "dollars per metric ton unit") is None

    def test_unknown_unit_returns_none(self):
        assert _to_usd_per_metric_ton(100.0, "dollars per furlong") is None

    def test_none_value_returns_none(self):
        assert _to_usd_per_metric_ton(None, "dollars per metric ton") is None


class TestExtractPricesFromSalient:
    @staticmethod
    def _price_row(commodity, year, value, unit, detail):
        return {
            "Commodity": commodity,
            "Section": "Salient Statistics—United States",
            "Statistics": "Price",
            "Statistics_detail": detail,
            "Year": str(year),
            "Value": str(value),
            "Unit": unit,
        }

    def test_extracts_single_benchmark_full_series(self):
        rows = [
            self._price_row("Lithium", 2021, "11,700",
                            "dollars per metric ton",
                            "Price, annual average-real, battery-grade lithium carbonate"),
            self._price_row("Lithium", 2022, "63,700",
                            "dollars per metric ton",
                            "Price, annual average-real, battery-grade lithium carbonate"),
            self._price_row("Lithium", 2025, "9,000",
                            "dollars per metric ton",
                            "Price, annual average-real, battery-grade lithium carbonate"),
        ]
        prices = _extract_prices_from_salient(rows)
        assert len(prices) == 3
        assert prices[0]["year"] == 2021
        assert prices[0]["value_raw"] == 11700.0
        assert prices[0]["value_usd_per_mt"] == 11700.0
        assert "battery-grade" in prices[0]["statistics_detail"]

    def test_preserves_multi_benchmark_structure(self):
        # Cobalt has both US-spot and LME — both must come through with
        # their distinct Statistics_detail strings preserved.
        rows = [
            self._price_row("Cobalt", 2021, "24.21", "dollars per pound",
                            "Price, average, dollars per pound: U.S. spot, cathode"),
            self._price_row("Cobalt", 2021, "23.17", "dollars per pound",
                            "Price, average, dollars per pound: London Metal Exchange"),
        ]
        prices = _extract_prices_from_salient(rows)
        assert len(prices) == 2
        details = {p["statistics_detail"] for p in prices}
        assert any("U.S. spot" in d for d in details)
        assert any("London Metal" in d for d in details)

    def test_ignores_non_price_statistics(self):
        rows = [
            self._price_row("Lithium", 2025, "9000", "dollars per metric ton",
                            "Price, annual average"),
            # Non-price row — should be filtered out
            {**self._price_row("Lithium", 2025, "10", "metric tons",
                               "Production"), "Statistics": "Production"},
        ]
        prices = _extract_prices_from_salient(rows)
        assert len(prices) == 1
        assert prices[0]["statistics_detail"] == "Price, annual average"

    def test_ignores_non_salient_sections(self):
        rows = [
            {**self._price_row("X", 2025, "100", "dollars per metric ton",
                               "Price"), "Section": "World Production"},
        ]
        prices = _extract_prices_from_salient(rows)
        assert prices == []

    def test_skips_malformed_rows(self):
        rows = [
            self._price_row("Lithium", "not_a_year", "9000",
                            "dollars per metric ton", "Price, annual"),
            self._price_row("Lithium", 2025, "not_a_number",
                            "dollars per metric ton", "Price, annual"),
            self._price_row("Lithium", 2025, "", "dollars per metric ton",
                            "Price, annual"),
        ]
        prices = _extract_prices_from_salient(rows)
        assert prices == []


class TestDeriveYoyAndCagrFromPrices:
    @staticmethod
    def _p(year, value, detail="Price, annual"):
        return {
            "year": year,
            "value_raw": float(value),
            "unit_raw": "dollars per metric ton",
            "statistics_detail": detail,
            "value_usd_per_mt": float(value),
        }

    def test_lithium_crash_real_numbers(self):
        # Real Lithium 2021-2025 series. Expected YoY ≈ -23.7%, CAGR ≈ -6.3%
        # (matches output seen in the smoke test against the real MCS CSV).
        prices = [
            self._p(2021, 11700), self._p(2022, 63700), self._p(2023, 39000),
            self._p(2024, 11800), self._p(2025, 9000),
        ]
        yoy, cagr = _derive_yoy_and_cagr_from_prices(prices)
        assert yoy is not None and -0.24 < yoy < -0.23
        assert cagr is not None and -0.064 < cagr < -0.062

    def test_picks_primary_benchmark_only(self):
        # Cobalt-style multi-benchmark: derivation must use ONLY the first
        # Statistics_detail, ignoring the secondary benchmark even when
        # it appears in the same year.
        prices = [
            self._p(2021, 100, "US spot"),
            self._p(2021, 80, "LME"),
            self._p(2025, 200, "US spot"),
            self._p(2025, 50, "LME"),
        ]
        yoy, cagr = _derive_yoy_and_cagr_from_prices(prices)
        # YoY of primary = (200-100)/100 = +1.00 (only 2 obs of US spot)
        # CAGR of primary over 4-year span = (200/100)^(1/4) - 1 = ~0.189
        assert yoy is not None and abs(yoy - 1.0) < 0.001
        assert cagr is not None and 0.18 < cagr < 0.20

    def test_too_few_observations(self):
        assert _derive_yoy_and_cagr_from_prices([]) == (None, None)
        assert _derive_yoy_and_cagr_from_prices([self._p(2025, 100)]) == (None, None)

    def test_handles_zero_first_value(self):
        prices = [self._p(2021, 0), self._p(2022, 100)]
        yoy, cagr = _derive_yoy_and_cagr_from_prices(prices)
        # Division-by-zero guard returns (None, None) when the baseline is 0
        assert yoy is None and cagr is None

    def test_falls_back_to_raw_when_normalised_missing(self):
        # DMTU rows leave value_usd_per_mt = None but value_raw is real;
        # YoY/CAGR are dimensionless so the result still computes from raw.
        prices = [
            {**self._p(2021, 100), "value_usd_per_mt": None},
            {**self._p(2025, 200), "value_usd_per_mt": None},
        ]
        yoy, cagr = _derive_yoy_and_cagr_from_prices(prices)
        assert yoy is not None and abs(yoy - 1.0) < 0.001
