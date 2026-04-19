"""Tests for facility / geo-event integration in the existing aggregators.

These cover the v3.0 fold-in paths: facilities feed ``country_concentration``
and ``structural_dependency``; geo-tagged events widen the trade event pool
beyond strictly company-tagged events.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.constants import RiskCategory
from app.services.scoring.evidence_aggregator import (
    derive_geopolitical_inputs,
    derive_material_inputs,
    derive_operational_inputs,
)
from app.services.scoring.evidence_query import EventWithRelevance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _exposure(material_id: int, country: str | None, score: float = 0.5):
    e = MagicMock()
    e.material_id = material_id
    e.source_geography = country
    e.exposure_score = score
    return e


def _event(
    event_id: int,
    title: str = "evt",
    severity: float = 0.5,
    confidence: float = 0.9,
    subtype: str | None = None,
    days_ago: int = 7,
) -> EventWithRelevance:
    metadata = {"event_subtype": subtype} if subtype else {}
    # Event date relative to AS_OF so recency multiplier stays inside [0.60, 1.20].
    ev_dt = datetime.combine(AS_OF - timedelta(days=days_ago), datetime.min.time(), timezone.utc)
    ev = SimpleNamespace(
        id=event_id,
        title=title,
        severity_score=severity,
        confidence_score=confidence,
        metadata_json=metadata,
        event_date=ev_dt,
    )
    return EventWithRelevance(event=ev, relevance_score=1.0)


def _facility(country: str, status: str = "operating"):
    return SimpleNamespace(country=country, status=status)


AS_OF = date(2025, 6, 1)


# ---------------------------------------------------------------------------
# derive_geopolitical_inputs
# ---------------------------------------------------------------------------

class TestGeopoliticalFacilityFold:
    def test_facility_only_no_exposures_uses_facility_share(self):
        facs = [_facility("CN"), _facility("CN"), _facility("US"), _facility("US")]
        cc, exp, tar = derive_geopolitical_inputs(
            trade_events=[],
            material_exposures=[],
            as_of_date=AS_OF,
            facilities=facs,
        )
        # source_share defaults to 0.5 (no exposures); facility_share = 0.5
        assert cc == pytest.approx(0.5)

    def test_facility_high_concentration_lifts_score(self):
        facs = [_facility("CN"), _facility("CN"), _facility("CN")]  # 100% HCG
        cc, _, _ = derive_geopolitical_inputs(
            trade_events=[],
            material_exposures=[],
            as_of_date=AS_OF,
            facilities=facs,
        )
        # 0.5 (source default) + 0.5 (facility 100%) → 0.75
        assert cc == pytest.approx(0.75)

    def test_facility_us_exposure_cn_balances(self):
        facs = [_facility("US"), _facility("US")]   # 0% HCG
        exp = [_exposure(1, "CN"), _exposure(2, "CN")]  # 100% HCG
        cc, _, _ = derive_geopolitical_inputs(
            trade_events=[],
            material_exposures=exp,
            as_of_date=AS_OF,
            facilities=facs,
        )
        # 0.5 * 1.0 + 0.5 * 0.0 = 0.5
        assert cc == pytest.approx(0.5)

    def test_geo_events_widen_export_pool(self):
        """An export-restriction event tagged via geography (not directly to
        the company) must still bump ``export_restriction_exposure``."""
        geo_ev = _event(
            event_id=42,
            title="China announces export controls on graphite",
            subtype="EXPORT_RESTRICTION",
            severity=0.9,
        )
        _, exp_score, _ = derive_geopolitical_inputs(
            trade_events=[],
            material_exposures=[_exposure(1, "CN")],
            as_of_date=AS_OF,
            geo_events=[geo_ev],
        )
        assert exp_score > 0.0

    def test_dedup_prevents_double_counting(self):
        """Same event in trade_events AND geo_events counted exactly once."""
        ev = _event(
            event_id=99,
            title="Tariff section 301 expansion",
            subtype="TARIFF",
            severity=0.6,
        )
        _, _, t1 = derive_geopolitical_inputs(
            trade_events=[ev],
            material_exposures=[],
            as_of_date=AS_OF,
        )
        _, _, t2 = derive_geopolitical_inputs(
            trade_events=[ev],
            material_exposures=[],
            as_of_date=AS_OF,
            geo_events=[ev],
        )
        assert t1 == pytest.approx(t2)


# ---------------------------------------------------------------------------
# derive_operational_inputs
# ---------------------------------------------------------------------------

class TestOperationalFacilityFold:
    def test_no_facilities_uses_event_default(self):
        struct, impacts = derive_operational_inputs(
            operational_events=[],
            as_of_date=AS_OF,
        )
        assert struct == pytest.approx(0.3)  # event default
        assert impacts == []

    def test_planned_facilities_lift_structural_dependency(self):
        facs = [
            _facility("CN", status="planned"),
            _facility("US", status="under_construction"),
            _facility("US", status="operating"),
        ]
        struct, _ = derive_operational_inputs(
            operational_events=[],
            as_of_date=AS_OF,
            facilities=facs,
        )
        # 2/3 non-operating; 0.4 * 2/3 = 0.267 → max(0.3, 0.267) = 0.3
        assert struct == pytest.approx(0.3)

    def test_majority_planned_facilities_dominate(self):
        facs = [_facility("CN", status="planned")] * 4 + [
            _facility("US", status="operating")
        ]
        struct, _ = derive_operational_inputs(
            operational_events=[],
            as_of_date=AS_OF,
            facilities=facs,
        )
        # 4/5 non-operating; 0.4 * 0.8 = 0.32 > 0.3 → 0.32
        assert struct == pytest.approx(0.32)

    def test_facility_country_events_added_to_impacts(self):
        country_ev = _event(event_id=7, title="Strike at CN refinery", severity=0.6)
        _, impacts = derive_operational_inputs(
            operational_events=[],
            as_of_date=AS_OF,
            facility_country_events=[country_ev],
        )
        assert len(impacts) == 1
        assert impacts[0] > 0.0


# ---------------------------------------------------------------------------
# derive_material_inputs (material-country event fold-in)
# ---------------------------------------------------------------------------

class TestMaterialCountryFold:
    def test_material_country_events_lift_trade_volatility(self):
        ev = _event(event_id=11, title="Lithium tariff", severity=0.8)
        _, _, vol_no = derive_material_inputs(
            material_exposures=[_exposure(1, "CN")],
            trade_events=[],
            as_of_date=AS_OF,
        )
        _, _, vol_with = derive_material_inputs(
            material_exposures=[_exposure(1, "CN")],
            trade_events=[],
            as_of_date=AS_OF,
            material_country_events=[ev],
        )
        # No company events → default 0.3; with event → impact-derived value.
        assert vol_no == pytest.approx(0.3)
        assert vol_with != pytest.approx(0.3)
        assert 0.0 < vol_with <= 1.0

    def test_dedup_company_and_material_country_events(self):
        ev = _event(event_id=23, title="Cobalt export ban", severity=0.7)
        _, _, vol_dup = derive_material_inputs(
            material_exposures=[_exposure(1, "CD")],
            trade_events=[ev],
            as_of_date=AS_OF,
            material_country_events=[ev],
        )
        _, _, vol_single = derive_material_inputs(
            material_exposures=[_exposure(1, "CD")],
            trade_events=[ev],
            as_of_date=AS_OF,
        )
        assert vol_dup == pytest.approx(vol_single)
