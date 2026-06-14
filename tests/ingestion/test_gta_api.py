"""Unit tests for GTA API parser (2026-06-11, Phase 1).

Tests cover the pure parsing logic and the intervention-type mappings.
Network calls to the real API are NOT exercised here — those would belong
in an integration-test suite with a recorded fixture.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.services.ingestion.gta import (
    BATTERY_HS_PREFIXES,
    DEFAULT_GTA_CATEGORY,
    GTA_INTERVENTION_CATEGORY_MAP,
    _INTERVENTION_ID_TO_SUBTYPE,
    _INTERVENTION_SUBTYPE_MAP,
    _apply_severity_modifiers,
    _resolve_iso3_jurisdiction,
    _resolve_iso3_list,
    _strip_html,
    compute_event_confidence,
    compute_tariff_magnitude_multiplier,
    parse_gta_api_response,
)


# ---------------------------------------------------------------------------
# Intervention-type mapping coverage
# ---------------------------------------------------------------------------

class TestSubtypeMapping:
    """Verify the canonical-name and integer-ID maps stay in sync."""

    def test_procurement_localisation_routes_to_procurement_policy(self):
        # The bug Nicole hit on 2026-06-11 — both data variants from the API
        # must map to PROCUREMENT_POLICY now.
        assert _INTERVENTION_SUBTYPE_MAP["Public procurement localisation"] == "PROCUREMENT_POLICY"
        assert _INTERVENTION_SUBTYPE_MAP["Public procurement, nes"]         == "PROCUREMENT_POLICY"

    def test_id_map_matches_string_map_intent(self):
        # Each ID in _INTERVENTION_ID_TO_SUBTYPE should resolve to a subtype
        # that some string in _INTERVENTION_SUBTYPE_MAP also maps to.
        for tid, subtype in _INTERVENTION_ID_TO_SUBTYPE.items():
            assert subtype in {
                "EXPORT_RESTRICTION", "IMPORT_DISRUPTION", "EXPORT_SUBSIDY",
                "PROCUREMENT_POLICY", "TRADE_DEFENSE", "FDI_RESTRICTION",
            }, f"ID {tid} maps to unknown subtype {subtype!r}"

    def test_unmapped_ids_documented(self):
        # These 11 IDs are intentionally unmapped (subtype stays NULL).
        unmapped = {9, 10, 13, 24, 31, 32, 33, 34, 60, 61, 77}
        for tid in unmapped:
            assert tid not in _INTERVENTION_ID_TO_SUBTYPE

    def test_export_restriction_family_complete(self):
        # All export-restriction intervention IDs should be present.
        for tid in (18, 19, 20, 21, 35, 37, 39, 69, 70, 74):
            assert _INTERVENTION_ID_TO_SUBTYPE[tid] == "EXPORT_RESTRICTION"

    def test_procurement_family_complete(self):
        for tid in (28, 29, 30, 40, 41, 42, 43, 57, 63, 64, 65, 66, 67, 68):
            assert _INTERVENTION_ID_TO_SUBTYPE[tid] == "PROCUREMENT_POLICY"


class TestCategoryMapping:
    def test_procurement_routes_to_regulatory_compliance(self):
        # Procurement family events should not pollute the geopolitical pillar.
        for name in ("Public procurement localisation", "Public procurement, nes",
                     "Local content requirement", "Local value added requirement"):
            assert GTA_INTERVENTION_CATEGORY_MAP[name] == "regulatory_compliance"

    def test_subsidy_routes_to_financial_pressure(self):
        for name in ("State loan", "Financial grant", "Production subsidy",
                     "Equity stake", "Lending support"):
            assert GTA_INTERVENTION_CATEGORY_MAP[name] == "financial_pressure"

    def test_export_ban_routes_to_geopolitical(self):
        assert GTA_INTERVENTION_CATEGORY_MAP["Export ban"] == "geopolitical_trade"


# ---------------------------------------------------------------------------
# parse_gta_api_response
# ---------------------------------------------------------------------------

def _make_api_row(**overrides):
    """Build a minimal valid API row, allowing per-test overrides."""
    base = {
        "intervention_id":         12345,
        "state_act_id":            98765,
        "state_act_title":         "China: Test export ban on graphite",
        "intervention_description": [{"text": "<p>Test description</p>"}],
        "intervention_url":         "https://globaltradealert.org/intervention/12345",
        "state_act_url":            "https://globaltradealert.org/state-act/98765",
        "gta_evaluation":           "Red",
        "implementing_jurisdictions": [{"id": 156, "name": "China", "iso": "CHN"}],
        "affected_jurisdictions":   [],
        "intervention_type":        "Export ban",
        "mast_chapter":             "P1",
        "affected_sectors":         [],
        "affected_products":        [{"product_id": 250410, "name": "Graphite"}],
        "date_announced":           "2025-06-01",
        "date_implemented":         "2025-07-01",
        "is_in_force":              True,
        "is_official_source":       True,
    }
    base.update(overrides)
    return base


class TestParseGtaApiResponse:
    def test_basic_row_parses(self):
        rows = [_make_api_row()]
        parsed = parse_gta_api_response(rows)
        assert len(parsed) == 1
        e = parsed[0]
        assert e["gta_id"] == 12345
        assert e["title"] == "China: Test export ban on graphite"
        assert e["implementing_iso2"] == "CN"
        assert e["intervention_type"] == "Export ban"
        assert e["risk_category"] == "geopolitical_trade"
        assert e["matched_hs_codes"] == ["250410"]
        assert e["in_force"] is True
        # HTML should be stripped from summary
        assert "<p>" not in e["summary"]
        assert "Test description" in e["summary"]
        # Source-mode flag for downstream debugging
        assert e["raw_row"]["_source_mode"] == "api"

    def test_red_filter_drops_non_red(self):
        rows = [_make_api_row(gta_evaluation="Green")]
        parsed = parse_gta_api_response(rows)
        assert parsed == []

    def test_hs_filter_drops_non_battery_products(self):
        # Pick a product clearly outside battery prefixes (e.g. textiles)
        non_battery = _make_api_row(
            affected_products=[{"product_id": 620111, "name": "Coats"}]
        )
        parsed = parse_gta_api_response([non_battery])
        assert parsed == []

    def test_hs_filter_can_be_disabled(self):
        non_battery = _make_api_row(
            affected_products=[{"product_id": 620111, "name": "Coats"}]
        )
        parsed = parse_gta_api_response([non_battery], skip_hs_filter=True)
        assert len(parsed) == 1
        assert parsed[0]["matched_hs_codes"] == ["620111"]

    def test_since_year_filter(self):
        old = _make_api_row(
            date_announced="2015-01-01",
            date_implemented="2015-01-15",
        )
        parsed = parse_gta_api_response([old], since_year=2018)
        assert parsed == []

    def test_missing_date_skips_row(self):
        rows = [_make_api_row(date_announced=None, date_implemented=None)]
        parsed = parse_gta_api_response(rows)
        assert parsed == []

    def test_missing_title_skips_row(self):
        rows = [_make_api_row(state_act_title="")]
        parsed = parse_gta_api_response(rows)
        assert parsed == []

    def test_multiple_jurisdictions_resolved(self):
        rows = [_make_api_row(
            affected_jurisdictions=[
                {"id": 392, "name": "Japan", "iso": "JPN"},
                {"id": 156, "name": "China", "iso": "CHN"},
            ],
        )]
        parsed = parse_gta_api_response(rows)
        assert parsed[0]["affected_iso2_list"] == ["JP", "CN"]

    def test_procurement_routes_to_regulatory_category(self):
        rows = [_make_api_row(
            intervention_type="Public procurement localisation",
            affected_products=[{"product_id": 250410, "name": "Graphite"}],
        )]
        parsed = parse_gta_api_response(rows)
        assert parsed[0]["risk_category"] == "regulatory_compliance"

    def test_subsidy_routes_to_financial_category(self):
        rows = [_make_api_row(
            intervention_type="Production subsidy",
            affected_products=[{"product_id": 283691, "name": "Lithium carbonate"}],
        )]
        parsed = parse_gta_api_response(rows)
        assert parsed[0]["risk_category"] == "financial_pressure"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestJurisdictionResolution:
    def test_iso3_to_iso2_via_name_lookup(self):
        result = _resolve_iso3_jurisdiction([
            {"id": 156, "name": "China", "iso": "CHN"}
        ])
        assert result == "CN"

    def test_unknown_jurisdiction_returns_none(self):
        result = _resolve_iso3_jurisdiction([
            {"id": 999, "name": "Atlantis", "iso": "ATL"}
        ])
        assert result is None

    def test_empty_list_returns_none(self):
        assert _resolve_iso3_jurisdiction([]) is None

    def test_list_resolution_dedupes_and_preserves_order(self):
        # Same country appearing twice should land once.
        result = _resolve_iso3_list([
            {"id": 156, "name": "China", "iso": "CHN"},
            {"id": 392, "name": "Japan", "iso": "JPN"},
            {"id": 156, "name": "China", "iso": "CHN"},
        ])
        assert result == ["CN", "JP"]


class TestStripHtml:
    def test_simple_tag_strip(self):
        assert _strip_html("<p>Hello world</p>") == "Hello world"

    def test_nested_tags(self):
        assert _strip_html("<div><b>Hello</b> <i>world</i></div>") == "Hello world"

    def test_empty_string(self):
        assert _strip_html("") == ""

    def test_whitespace_normalised(self):
        assert _strip_html("<p>Hello\n\n  world</p>\r\n") == "Hello world"


# ---------------------------------------------------------------------------
# Precision upgrades (2026-06-11): tariff magnitude + confidence calibration
# ---------------------------------------------------------------------------

class TestTariffMagnitudeMultiplier:
    def test_no_change_returns_one(self):
        # Same level → no bump
        assert compute_tariff_magnitude_multiplier(5.0, 5.0) == 1.0

    def test_decrease_returns_one(self):
        # Tariff lowered — we don't down-weight (severity unchanged)
        assert compute_tariff_magnitude_multiplier(10.0, 5.0) == 1.0

    def test_modest_increase(self):
        # 5% → 7.5%  →  log10(1.5) ≈ 0.176
        m = compute_tariff_magnitude_multiplier(5.0, 7.5)
        assert m == pytest.approx(1.176, abs=0.005)

    def test_doubling_tariff(self):
        # 5% → 10%  →  log10(2) ≈ 0.301
        m = compute_tariff_magnitude_multiplier(5.0, 10.0)
        assert m == pytest.approx(1.301, abs=0.005)

    def test_5x_tariff_caps_at_half_bucket(self):
        # 5% → 25%  →  log10(5) = 0.699  →  cap 0.5
        m = compute_tariff_magnitude_multiplier(5.0, 25.0)
        assert m == pytest.approx(1.5)

    def test_10x_tariff_still_capped(self):
        # 5% → 50%  →  log10(10) = 1.0  →  cap holds at 0.5
        m = compute_tariff_magnitude_multiplier(5.0, 50.0)
        assert m == pytest.approx(1.5)

    def test_null_inputs_default_to_one(self):
        assert compute_tariff_magnitude_multiplier(None, 25.0) == 1.0
        assert compute_tariff_magnitude_multiplier(5.0, None) == 1.0
        assert compute_tariff_magnitude_multiplier(None, None) == 1.0

    def test_zero_or_negative_inputs_default_to_one(self):
        # Defensive — can't take log of 0 or negatives
        assert compute_tariff_magnitude_multiplier(0.0, 25.0) == 1.0
        assert compute_tariff_magnitude_multiplier(5.0, 0.0) == 1.0
        assert compute_tariff_magnitude_multiplier(-1.0, 25.0) == 1.0


class TestEventConfidence:
    def test_all_none_returns_legacy_default(self):
        # CSV path — none of the API-only signals available
        assert compute_event_confidence(None, None, None) == 0.90

    def test_official_source_base_confidence(self):
        assert compute_event_confidence(True, None, None) == 0.90

    def test_unofficial_source_drops_confidence(self):
        assert compute_event_confidence(False, None, None) == 0.60

    def test_multi_source_bump(self):
        # Official + 3 citations → 0.90 + 0.05 = 0.95 (ceiling)
        assert compute_event_confidence(True, 3, None) == 0.95
        # Unofficial + 2 citations → 0.60 + 0.05 = 0.65
        assert compute_event_confidence(False, 2, None) == 0.65

    def test_single_source_no_bump(self):
        # source_count=1 doesn't qualify for the multi-source bump
        assert compute_event_confidence(True, 1, None) == 0.90

    def test_inferred_jurisdictions_penalty(self):
        # Official source but inferred attribution → 0.90 - 0.10 = 0.80
        assert compute_event_confidence(True, None, "Inferred") == 0.80
        # Case-insensitive
        assert compute_event_confidence(True, None, "INFERRED") == 0.80

    def test_combined_inferred_unofficial(self):
        # Unofficial + inferred — both penalties → 0.60 - 0.10 = 0.50 (floor)
        assert compute_event_confidence(False, None, "Inferred") == 0.50

    def test_combined_signals(self):
        # Unofficial + multi-source + inferred → 0.60 + 0.05 - 0.10 = 0.55
        assert compute_event_confidence(False, 2, "Inferred") == 0.55

    def test_clamped_to_ceiling(self):
        # Even if hypothetical signals overshoot
        assert compute_event_confidence(True, 100, None) == 0.95

    def test_clamped_to_floor(self):
        assert compute_event_confidence(False, None, "Inferred") >= 0.50


class TestSeverityWithTariffMultiplier:
    def test_default_multiplier_does_not_change_severity(self):
        # Base 0.7, no implementation level, not horizontal, no tariff bump
        result = _apply_severity_modifiers(
            0.7, implementation_level=None, is_horizontal=False,
        )
        assert result == 0.7

    def test_tariff_multiplier_applied(self):
        # Base 0.7 × 1.3 = 0.91, within [0,1] cap
        result = _apply_severity_modifiers(
            0.7, implementation_level=None, is_horizontal=False,
            tariff_magnitude_multiplier=1.3,
        )
        assert result == pytest.approx(0.91)

    def test_tariff_multiplier_clamped_at_one(self):
        # Base 0.9 (export ban) × 1.5 = 1.35 → clamped to 1.0
        result = _apply_severity_modifiers(
            0.9, implementation_level=None, is_horizontal=False,
            tariff_magnitude_multiplier=1.5,
        )
        assert result == 1.0

    def test_tariff_and_other_multipliers_compose(self):
        # Base 0.7 × subnational 0.5 × tariff 1.3 = 0.455
        result = _apply_severity_modifiers(
            0.7, implementation_level="subnational", is_horizontal=False,
            tariff_magnitude_multiplier=1.3,
        )
        assert result == pytest.approx(0.455)


# ---------------------------------------------------------------------------
# Precision: parse_gta_api_response captures new fields
# ---------------------------------------------------------------------------

class TestParseApiCapturesNewFields:
    def _row_with_tariff(self, prior, new):
        return _make_api_row(
            intervention_type="Import tariff",
            affected_products=[{
                "product_id": 280530,
                "name": "Cerium compounds",
                "prior_level": prior,
                "new_level":   new,
                "unit":        "ad-valorem-percent",
                "date_implemented": "2025-07-01",
                "date_removed":     None,
            }],
            is_official_source=True,
            state_act_source=[{"text": "source 1"}, {"text": "source 2"}],
            mast_subchapter="P6 Antidumping",
        )

    def test_tariff_magnitude_captured(self):
        row = self._row_with_tariff(5.0, 25.0)
        parsed = parse_gta_api_response([row])
        assert len(parsed) == 1
        assert parsed[0]["tariff_magnitude_multiplier"] == pytest.approx(1.5)
        # Per-product detail preserved in raw_row
        per_prod = parsed[0]["raw_row"]["per_product"]
        assert per_prod[0]["prior_level"] == 5.0
        assert per_prod[0]["new_level"]   == 25.0

    def test_no_tariff_levels_yields_default_multiplier(self):
        row = self._row_with_tariff(None, None)
        parsed = parse_gta_api_response([row])
        assert parsed[0]["tariff_magnitude_multiplier"] == 1.0

    def test_confidence_override_computed(self):
        # Official + 2 sources → 0.95
        row = self._row_with_tariff(5.0, 10.0)
        parsed = parse_gta_api_response([row])
        assert parsed[0]["confidence_override"] == 0.95

    def test_mast_subchapter_in_metadata(self):
        row = self._row_with_tariff(5.0, 10.0)
        parsed = parse_gta_api_response([row])
        assert parsed[0]["raw_row"]["mast_subchapter"] == "P6 Antidumping"

    def test_state_act_id_in_metadata(self):
        row = self._row_with_tariff(5.0, 10.0)
        parsed = parse_gta_api_response([row])
        assert parsed[0]["raw_row"]["state_act_id"] == 98765

    def test_inferred_jurisdictions_drops_confidence(self):
        row = _make_api_row(
            is_official_source=True,
            inferred_jurisdictions="Inferred",
            state_act_source=[],
        )
        parsed = parse_gta_api_response([row])
        # Official + 0 sources + inferred = 0.90 - 0.10 = 0.80
        assert parsed[0]["confidence_override"] == 0.80

    def test_latest_action_used_as_event_date_when_more_recent(self):
        row = _make_api_row(
            date_implemented="2025-01-01",
            latest_action_date="2026-03-15",
        )
        parsed = parse_gta_api_response([row])
        # latest_action captured for downstream substitution
        assert parsed[0]["latest_action_date"] is not None
        assert parsed[0]["latest_action_date"].date() == date(2026, 3, 15)
