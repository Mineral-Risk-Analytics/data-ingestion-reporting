"""Unit tests for the 11.4-Op per-sub-input diagnostic and the
severity-default fix.

The audit (2026-06-06) added a 5th return value to
``_derive_market_operational_inputs`` (sub_input_diagnostic) and changed
the ``severity_score or 0.5`` midpoint fallback to ``or 0.0`` at three
callsites in market_aggregator.  This file tests the Operational pillar
diagnostic surface; the severity-default fix is exercised indirectly via
the null_severity_event_count assertion.

Key behaviours under test:

* structural_dependency: data_backed=True ONLY for the MRDS path, even
  though event_derived produces a non-zero value.  Source label exposes
  which tier fired.

* event_impacts: counts surfaced for (operational events / G-Cov-2
  export-restriction shadows / null-severity events) so partner UI can
  see exactly which classes of evidence contributed.

* scoring_profile: "events_only" when struct_dep is None, otherwise
  "structural_plus_events" — makes the 100%-events redistribution
  visible without forcing the partner to read code.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.constants import RiskCategory
from app.services.scoring.evidence_query import EventWithRelevance
from app.services.scoring.market_aggregator import (
    _derive_market_operational_inputs,
)


AS_OF = date(2026, 6, 4)


def _event(
    event_id: int = 1,
    title: str = "Operational disruption",
    severity: float | None = 0.5,
    confidence: float = 0.9,
    subtype: str | None = None,
    days_ago: int = 7,
) -> EventWithRelevance:
    from datetime import datetime, timedelta, timezone
    ev_dt = datetime.combine(
        AS_OF - timedelta(days=days_ago), datetime.min.time(), timezone.utc,
    )
    ev = SimpleNamespace(
        id=event_id, title=title, severity_score=severity,
        confidence_score=confidence,
        metadata_json={"event_subtype": subtype} if subtype else {},
        event_date=ev_dt, event_subtype=subtype,
    )
    return EventWithRelevance(event=ev, relevance_score=1.0)


# ---------------------------------------------------------------------------
# structural_dependency diagnostic
# ---------------------------------------------------------------------------

class TestStructuralDependencyDiagnostic:
    """3-way source: mrds_geography_stage_weighted / event_derived / no_signal."""

    def test_no_signal_when_empty_db(self, sqlite_session):
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            operational_events=[], as_of_date=AS_OF,
        )
        struct_dep, _, dep_source, _, diag = result
        assert struct_dep is None
        assert dep_source == "no_signal"
        assert diag["structural_dependency"]["data_backed"] is False
        assert diag["structural_dependency"]["source"] == "no_signal"

    def test_event_derived_not_marked_data_backed(self, sqlite_session):
        """When SINGLE_SOURCE / CAPACITY_CONSTRAINT events fire Tier 3,
        struct_dep gets a numeric value but data_backed should remain
        False — events aren't structural data."""
        events = [_event(subtype="SINGLE_SOURCE", severity=0.8)]
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CN",
            operational_events=events, as_of_date=AS_OF,
        )
        struct_dep, _, dep_source, _, diag = result
        assert struct_dep == pytest.approx(0.8)
        assert dep_source == "event_derived"
        assert diag["structural_dependency"]["data_backed"] is False
        assert diag["structural_dependency"]["source"] == "event_derived"

    def test_mrds_geography_marked_data_backed(self, sqlite_session):
        """When MRDS facilities exist for the (material, geography) pair,
        Tier 1 fires and data_backed should be True."""
        from app.models.facility import Facility, FacilityMaterialLink
        from app.models.supply import Material

        m = Material(canonical_name="TestMat")
        sqlite_session.add(m)
        sqlite_session.flush()

        f = Facility(
            name="X", country="CN", facility_type="mine", status="operating",
        )
        sqlite_session.add(f)
        sqlite_session.flush()

        link = FacilityMaterialLink(
            facility_id=f.id, material_id=m.id,
            supply_chain_stage="ore",
        )
        sqlite_session.add(link)
        sqlite_session.commit()

        result = _derive_market_operational_inputs(
            sqlite_session, material_id=m.id, geography_code="CN",
            operational_events=[], as_of_date=AS_OF,
        )
        _, _, dep_source, _, diag = result
        assert dep_source == "mrds_geography_stage_weighted"
        assert diag["structural_dependency"]["data_backed"] is True
        assert diag["structural_dependency"]["source"] == "mrds_geography_stage_weighted"


# ---------------------------------------------------------------------------
# event_impacts diagnostic
# ---------------------------------------------------------------------------

class TestEventImpactsDiagnostic:
    def test_no_events_zero_count(self, sqlite_session):
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            operational_events=[], as_of_date=AS_OF,
        )
        _, _, _, _, diag = result
        assert diag["event_impacts"]["data_backed"] is False
        assert diag["event_impacts"]["operational_event_count"] == 0
        assert diag["event_impacts"]["export_restriction_event_count"] == 0
        assert diag["event_impacts"]["null_severity_event_count"] == 0

    def test_operational_event_count_surfaced(self, sqlite_session):
        events = [
            _event(event_id=1, severity=0.5),
            _event(event_id=2, severity=0.6),
            _event(event_id=3, severity=0.4),
        ]
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CN",
            operational_events=events, as_of_date=AS_OF,
        )
        _, _, _, _, diag = result
        assert diag["event_impacts"]["operational_event_count"] == 3
        assert diag["event_impacts"]["data_backed"] is True

    def test_null_severity_event_counter_fires(self, sqlite_session):
        """The 11.4-Op partner-visible flag: when an event lands with
        severity_score=None, it now contributes 0.0 (was 0.5 pre-fix).
        The counter exposes this so partner can decide if the events
        should be backfilled by the parser."""
        events = [
            _event(event_id=1, severity=0.5),
            _event(event_id=2, severity=None),    # the 11.4-Op flag case
            _event(event_id=3, severity=None),
        ]
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CN",
            operational_events=events, as_of_date=AS_OF,
        )
        _, _, _, _, diag = result
        assert diag["event_impacts"]["operational_event_count"] == 3
        assert diag["event_impacts"]["null_severity_event_count"] == 2


# ---------------------------------------------------------------------------
# scoring_profile diagnostic
# ---------------------------------------------------------------------------

class TestScoringProfileDiagnostic:
    """Makes the 100%-events vs 40/60 redistribution visible."""

    def test_no_struct_dep_is_events_only(self, sqlite_session):
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            operational_events=[], as_of_date=AS_OF,
        )
        struct_dep, _, _, _, diag = result
        assert struct_dep is None
        assert diag["scoring_profile"] == "events_only"

    def test_event_derived_struct_dep_is_structural_plus_events(self, sqlite_session):
        events = [_event(subtype="SINGLE_SOURCE", severity=0.6)]
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CN",
            operational_events=events, as_of_date=AS_OF,
        )
        struct_dep, _, _, _, diag = result
        assert struct_dep is not None
        assert diag["scoring_profile"] == "structural_plus_events"


# ---------------------------------------------------------------------------
# Diagnostic shape stability
# ---------------------------------------------------------------------------

class TestDiagnosticShape:
    def test_top_level_keys(self, sqlite_session):
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            operational_events=[], as_of_date=AS_OF,
        )
        diag = result[4]
        assert set(diag.keys()) == {
            "structural_dependency", "event_impacts", "scoring_profile",
        }

    def test_structural_dependency_inner_keys(self, sqlite_session):
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            operational_events=[], as_of_date=AS_OF,
        )
        diag = result[4]
        assert set(diag["structural_dependency"].keys()) == {
            "data_backed", "source",
        }

    def test_event_impacts_inner_keys(self, sqlite_session):
        result = _derive_market_operational_inputs(
            sqlite_session, material_id=1, geography_code="CD",
            operational_events=[], as_of_date=AS_OF,
        )
        diag = result[4]
        assert set(diag["event_impacts"].keys()) == {
            "data_backed",
            "operational_event_count",
            "export_restriction_event_count",
            "null_severity_event_count",
        }
