"""Unit tests for the 11.4-Geo per-sub-input diagnostic dict.

The audit (2026-06-06) added a 6th return value to
``_derive_market_geopolitical_inputs``: a ``sub_input_diagnostic`` dict
mirroring the Material pillar's 11.4-C pattern.  Unlike Material, the
Geopolitical pillar's numeric defaults were already correct — the
diagnostic is the entire deliverable.

Key behaviours under test:

* country_concentration: 3-way source label (mcs_share / facility_floor
  / no_data); data_backed is True ONLY for mcs_share — facility_floor is
  a presence marker, not a quantitative share.

* export_restriction / tariff: 4-way source attribution (both /
  events_only / hs_only / neither) so partner UI can see which path
  contributed signal when ``method == "max_with_hs_nodes"``.

* subsidy: data_backed=False is the partner-visible flag for the
  3-component scoring asymmetric default.  scoring_profile string makes
  the same fact human-readable.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.constants import RiskCategory
from app.services.scoring.evidence_query import EventWithRelevance
from app.services.scoring.market_aggregator import (
    _classify_path_source,
    _derive_market_geopolitical_inputs,
)


AS_OF = date(2026, 6, 4)


def _event(
    event_id: int = 1,
    title: str = "Tariff hike on cobalt sulphate",
    severity: float = 0.5,
    confidence: float = 0.9,
    subtype: str | None = "TARIFF",
    days_ago: int = 7,
) -> EventWithRelevance:
    from datetime import datetime, timedelta, timezone
    ev_dt = datetime.combine(
        AS_OF - timedelta(days=days_ago), datetime.min.time(), timezone.utc,
    )
    metadata = {"event_subtype": subtype} if subtype else {}
    ev = SimpleNamespace(
        id=event_id, title=title, severity_score=severity,
        confidence_score=confidence, metadata_json=metadata,
        event_date=ev_dt, event_subtype=subtype,
    )
    return EventWithRelevance(event=ev, relevance_score=1.0)


def _hs_node(stage: str, *, tariff: float = 0.0, export: float = 0.0):
    """Stub an HsCodeGeographyRiskScore-like row with a hs_mapping.stage."""
    hs_mapping = SimpleNamespace(supply_chain_stage=stage)
    return SimpleNamespace(
        hs_mapping=hs_mapping,
        tariff_exposure=tariff,
        export_restriction=export,
    )


# ---------------------------------------------------------------------------
# _classify_path_source pure helper
# ---------------------------------------------------------------------------

class TestClassifyPathSource:
    """The 4-way source label that drives export/tariff "source" field."""

    def test_neither_when_both_zero(self):
        assert _classify_path_source(
            event_value=0.0, hs_value=0.0, hs_path_fired=True,
        ) == "neither"

    def test_neither_when_hs_path_not_fired_and_no_events(self):
        assert _classify_path_source(
            event_value=0.0, hs_value=0.0, hs_path_fired=False,
        ) == "neither"

    def test_events_only_when_only_event_nonzero(self):
        assert _classify_path_source(
            event_value=0.3, hs_value=0.0, hs_path_fired=True,
        ) == "events_only"

    def test_events_only_when_hs_path_not_fired(self):
        assert _classify_path_source(
            event_value=0.3, hs_value=0.5, hs_path_fired=False,
        ) == "events_only"  # hs_value irrelevant when path didn't fire

    def test_hs_only_when_only_hs_nonzero(self):
        assert _classify_path_source(
            event_value=0.0, hs_value=0.4, hs_path_fired=True,
        ) == "hs_only"

    def test_both_when_both_nonzero(self):
        assert _classify_path_source(
            event_value=0.2, hs_value=0.4, hs_path_fired=True,
        ) == "both"


# ---------------------------------------------------------------------------
# country_concentration diagnostic
# ---------------------------------------------------------------------------

class TestCountryConcentrationDiagnostic:
    """3-way source label: mcs_share / facility_floor / no_data."""

    def test_no_signal_when_empty_db(self, sqlite_session):
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=None,
        )
        ctry_conc, *_, diag = result
        assert ctry_conc == 0.0
        assert diag["country_concentration"]["data_backed"] is False
        assert diag["country_concentration"]["source"] == "no_data"

    def test_facility_floor_not_marked_data_backed(self, sqlite_session):
        """Facility presence is a structural marker, not a quantitative
        share — diagnostic should NOT call it data_backed even though it
        produces a non-zero value (0.02)."""
        # Seed a Facility + FacilityMaterialLink to trigger the floor branch.
        from app.models.facility import Facility, FacilityMaterialLink
        from app.models.supply import Material

        m = Material(canonical_name="TestMat")
        sqlite_session.add(m)
        sqlite_session.flush()

        f = Facility(name="X", country="CD", facility_type="mine")
        sqlite_session.add(f)
        sqlite_session.flush()

        link = FacilityMaterialLink(facility_id=f.id, material_id=m.id)
        sqlite_session.add(link)
        sqlite_session.commit()

        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=m.id, geography_code="CD",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=None,
        )
        ctry_conc, *_, diag = result
        assert ctry_conc == pytest.approx(0.02)  # _FACILITY_PRESENCE_FLOOR
        assert diag["country_concentration"]["data_backed"] is False
        assert diag["country_concentration"]["source"] == "facility_floor"

    def test_mcs_share_marked_data_backed(self, sqlite_session):
        from app.models.supply import Material, MaterialProductionShare

        m = Material(canonical_name="TestMat")
        sqlite_session.add(m)
        sqlite_session.flush()

        share = MaterialProductionShare(
            material_id=m.id, country_code="CN",
            production_share=0.70, reference_year=2024,
        )
        sqlite_session.add(share)
        sqlite_session.commit()

        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=m.id, geography_code="CN",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=None,
        )
        ctry_conc, *_, diag = result
        assert ctry_conc == pytest.approx(0.70)
        assert diag["country_concentration"]["data_backed"] is True
        assert diag["country_concentration"]["source"] == "mcs_share"


# ---------------------------------------------------------------------------
# export_restriction / tariff diagnostic
# ---------------------------------------------------------------------------

class TestExportTariffDiagnostic:
    """Source-attribution for the max-combined sub-inputs."""

    def test_no_data_marks_neither(self, sqlite_session):
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=None,
        )
        _, exp_rest, tariff, *_, diag = result
        assert exp_rest == 0.0
        assert tariff == 0.0
        assert diag["export_restriction"]["data_backed"] is False
        assert diag["export_restriction"]["source"] == "neither"
        assert diag["tariff"]["data_backed"] is False
        assert diag["tariff"]["source"] == "neither"

    def test_tariff_event_marks_events_only(self, sqlite_session):
        events = [_event(subtype="TARIFF", severity=0.8)]
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CN",
            geo_trade_events=events, as_of_date=AS_OF, eligible_nodes=None,
        )
        _, _, tariff, *_, diag = result
        assert tariff > 0.0
        assert diag["tariff"]["data_backed"] is True
        assert diag["tariff"]["source"] == "events_only"
        # No export events seeded → export is "neither".
        assert diag["export_restriction"]["source"] == "neither"

    def test_hs_node_path_only_marks_hs_only(self, sqlite_session):
        """When events are empty but HS nodes have signal."""
        nodes = [
            _hs_node("ore", tariff=0.4, export=0.3),
            _hs_node("refined", tariff=0.5, export=0.2),
        ]
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CN",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=nodes,
        )
        _, exp_rest, tariff, _, method, diag = result
        assert method == "max_with_hs_nodes"
        assert tariff > 0.0
        assert exp_rest > 0.0
        assert diag["tariff"]["source"] == "hs_only"
        assert diag["export_restriction"]["source"] == "hs_only"
        assert diag["tariff"]["data_backed"] is True
        assert diag["export_restriction"]["data_backed"] is True

    def test_both_paths_marks_both(self, sqlite_session):
        events = [_event(subtype="TARIFF", severity=0.6)]
        nodes = [
            _hs_node("ore", tariff=0.4),
            _hs_node("refined", tariff=0.3),
        ]
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CN",
            geo_trade_events=events, as_of_date=AS_OF, eligible_nodes=nodes,
        )
        _, _, _, _, method, diag = result
        assert method == "max_with_hs_nodes"
        assert diag["tariff"]["source"] == "both"


# ---------------------------------------------------------------------------
# subsidy diagnostic — the asymmetric-default flag
# ---------------------------------------------------------------------------

class TestSubsidyDiagnostic:
    """When subsidy_distortion is None, scoring falls to the 3-component
    profile and implicitly redistributes the 10% subsidy weight.  The
    diagnostic surfaces this state without changing the math."""

    def test_no_subsidy_events_flags_three_component(self, sqlite_session):
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CN",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=None,
        )
        _, _, _, sub_dist, _, diag = result
        assert sub_dist is None
        assert diag["subsidy"]["data_backed"] is False
        assert diag["subsidy"]["scoring_profile"] == "3_component"

    def test_subsidy_event_flags_four_component(self, sqlite_session):
        events = [_event(subtype="EXPORT_SUBSIDY", severity=0.7)]
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CN",
            geo_trade_events=events, as_of_date=AS_OF, eligible_nodes=None,
        )
        _, _, _, sub_dist, _, diag = result
        assert sub_dist is not None
        assert sub_dist > 0.0
        assert diag["subsidy"]["data_backed"] is True
        assert diag["subsidy"]["scoring_profile"] == "4_component"


# ---------------------------------------------------------------------------
# Diagnostic shape stability
# ---------------------------------------------------------------------------

class TestDiagnosticShape:
    """Top-level + inner key contract — partner UI relies on this."""

    def test_top_level_keys(self, sqlite_session):
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=None,
        )
        _, _, _, _, _, diag = result
        assert set(diag.keys()) == {
            "country_concentration", "export_restriction", "tariff", "subsidy",
        }

    def test_country_concentration_inner_keys(self, sqlite_session):
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=None,
        )
        diag = result[5]
        assert set(diag["country_concentration"].keys()) == {"data_backed", "source"}

    def test_subsidy_inner_keys(self, sqlite_session):
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=None,
        )
        diag = result[5]
        assert set(diag["subsidy"].keys()) == {"data_backed", "scoring_profile"}

    def test_export_tariff_inner_keys(self, sqlite_session):
        result = _derive_market_geopolitical_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            geo_trade_events=[], as_of_date=AS_OF, eligible_nodes=None,
        )
        diag = result[5]
        assert set(diag["export_restriction"].keys()) == {"data_backed", "source"}
        assert set(diag["tariff"].keys()) == {"data_backed", "source"}
