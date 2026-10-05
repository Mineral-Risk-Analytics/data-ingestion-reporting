"""067 piece 3 — standing regulation floors under the geo pillar's sub-inputs.

The floor term (regulation_pillar_reassignment.md §3.3): a regime in force
guarantees a minimum export/tariff exposure that survives the 730-day
evidence window. Combined as ``max(event, hs, floor)`` — fresh events win
when they exceed it; nothing sums, so one regime never double-counts.

Covers: floor applies with no event signal; events beat the floor; the
enforcement-weight scaling (065); the enacted/effective status gate; the
geography and material-scope gates; MAX across competing regimes; the
diagnostic's ``standing_floor`` entry; and the no-floors no-change
guarantee.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.models.regulatory import Regulation, RegulationMaterialScope, RiskEvent
from app.services.scoring.evidence_query import EventWithRelevance
from app.models.supply import Material
from app.services.scoring.market_aggregator import (
    _derive_market_geopolitical_inputs,
    _derive_standing_floors,
)

AS_OF = date(2026, 9, 22)


def _mk_material(session, name="Nickel"):
    m = Material(canonical_name=name)
    session.add(m)
    session.flush()
    return m


def _mk_reg(session, key, *, geo="ID", status="effective", exp=None, tar=None,
            all_mats=False, enf=None, material_id=None, scope_type="banned"):
    reg = Regulation(
        regulation_key=key, geography=geo, status=status,
        pillar="geopolitical_trade",
        standing_export_restriction=exp, standing_tariff_exposure=tar,
        applies_all_materials=all_mats, material_enforcement_weights=enf,
    )
    session.add(reg)
    session.flush()
    if material_id is not None:
        session.add(RegulationMaterialScope(
            regulation_id=reg.id, material_id=material_id,
            scope_type=scope_type,
        ))
        session.flush()
    return reg


class TestDeriveStandingFloors:
    def test_scoped_floor_found(self, sqlite_session):
        m = _mk_material(sqlite_session)
        _mk_reg(sqlite_session, "ID_NICKEL_ORE_BAN", exp=0.35, material_id=m.id)
        fe, fe_key, ft, ft_key = _derive_standing_floors(sqlite_session, m.id, "ID")
        assert (fe, fe_key) == (0.35, "ID_NICKEL_ORE_BAN")
        assert (ft, ft_key) == (0.0, None)

    def test_all_goods_floor_found(self, sqlite_session):
        m = _mk_material(sqlite_session)
        _mk_reg(sqlite_session, "ALL_GOODS_CTRL", geo="CN", exp=0.5, all_mats=True)
        fe, fe_key, _, _ = _derive_standing_floors(sqlite_session, m.id, "CN")
        assert (fe, fe_key) == (0.5, "ALL_GOODS_CTRL")

    def test_enforcement_weight_scales_floor(self, sqlite_session):
        m = _mk_material(sqlite_session, "Gallium")
        _mk_reg(sqlite_session, "CN_MINOR_METALS", geo="CN", exp=0.5,
                all_mats=True, enf={"Gallium": 0.8, "DEFAULT": 0.2})
        fe, fe_key, _, _ = _derive_standing_floors(sqlite_session, m.id, "CN")
        assert fe == 0.5 * 0.8
        assert fe_key == "CN_MINOR_METALS"

    def test_status_gate(self, sqlite_session):
        m = _mk_material(sqlite_session)
        _mk_reg(sqlite_session, "SUSPENDED_BAN", status="suspended",
                exp=0.5, material_id=m.id)
        _mk_reg(sqlite_session, "PROPOSED_BAN", status="proposed",
                exp=0.6, material_id=m.id)
        fe, fe_key, _, _ = _derive_standing_floors(sqlite_session, m.id, "ID")
        assert (fe, fe_key) == (0.0, None)

    def test_geography_gate(self, sqlite_session):
        m = _mk_material(sqlite_session)
        _mk_reg(sqlite_session, "ZW_BAN", geo="ZW", exp=0.45, material_id=m.id)
        fe, fe_key, _, _ = _derive_standing_floors(sqlite_session, m.id, "ID")
        assert (fe, fe_key) == (0.0, None)

    def test_material_scope_gate(self, sqlite_session):
        nickel = _mk_material(sqlite_session, "Nickel")
        cobalt = _mk_material(sqlite_session, "Cobalt")
        _mk_reg(sqlite_session, "ID_NICKEL_ORE_BAN", exp=0.35,
                material_id=nickel.id)
        fe, fe_key, _, _ = _derive_standing_floors(sqlite_session, cobalt.id, "ID")
        assert (fe, fe_key) == (0.0, None)

    def test_max_across_regimes_and_tariff_side(self, sqlite_session):
        m = _mk_material(sqlite_session, "Aluminum")
        _mk_reg(sqlite_session, "WEAK_CTRL", geo="US", exp=0.2, tar=0.4,
                material_id=m.id)
        _mk_reg(sqlite_session, "STRONG_CTRL", geo="US", exp=0.3, tar=0.25,
                material_id=m.id)
        fe, fe_key, ft, ft_key = _derive_standing_floors(sqlite_session, m.id, "US")
        assert (fe, fe_key) == (0.3, "STRONG_CTRL")   # per-floor winners differ
        assert (ft, ft_key) == (0.4, "WEAK_CTRL")


class TestFloorInGeopoliticalInputs:
    """Through the real _derive_market_geopolitical_inputs (empty events)."""

    def _derive(self, session, material_id, geo):
        return _derive_market_geopolitical_inputs(
            session, material_id, geo, geo_trade_events=[], as_of_date=AS_OF,
        )

    def test_floor_sets_exposure_when_no_events(self, sqlite_session):
        m = _mk_material(sqlite_session)
        _mk_reg(sqlite_session, "ID_NICKEL_ORE_BAN", exp=0.35, material_id=m.id)
        _conc, exp, tar, _sub, _method, diag = self._derive(
            sqlite_session, m.id, "ID"
        )
        assert exp == 0.35
        assert tar == 0.0
        sf = diag["export_restriction"]["standing_floor"]
        assert sf == {
            "regulation_key": "ID_NICKEL_ORE_BAN", "value": 0.35, "applied": True,
        }
        assert diag["export_restriction"]["data_backed"] is True

    def test_no_floor_leaves_behavior_unchanged(self, sqlite_session):
        m = _mk_material(sqlite_session)
        _conc, exp, tar, _sub, _method, diag = self._derive(
            sqlite_session, m.id, "ID"
        )
        assert exp == 0.0 and tar == 0.0
        sf = diag["export_restriction"]["standing_floor"]
        assert sf == {"regulation_key": None, "value": 0.0, "applied": False}
        assert diag["export_restriction"]["data_backed"] is False

    def test_fresh_events_beat_a_low_floor(self, sqlite_session):
        """The escalation case: live event signal above the floor wins and
        the diagnostic reports the floor as present but NOT applied."""
        m = _mk_material(sqlite_session)
        _mk_reg(sqlite_session, "ID_NICKEL_ORE_BAN", exp=0.10, material_id=m.id)
        ban_event = EventWithRelevance(
            event=RiskEvent(
                event_type="MANUAL",
                title="Indonesia widens export restriction on nickel",
                event_date=datetime.now(timezone.utc) - timedelta(days=30),
                severity_score=0.9,
                confidence_score=0.85,
                risk_categories_json=["geopolitical_trade"],
            ),
            relevance_score=1.0,
        )
        _conc, exp, _tar, _sub, _method, diag = _derive_market_geopolitical_inputs(
            sqlite_session, m.id, "ID", geo_trade_events=[ban_event],
            as_of_date=AS_OF,
        )
        assert exp > 0.10                       # event signal won the max()
        sf = diag["export_restriction"]["standing_floor"]
        assert sf["regulation_key"] == "ID_NICKEL_ORE_BAN"
        assert sf["value"] == 0.10
        assert sf["applied"] is False

    def test_tariff_floor_flows_through(self, sqlite_session):
        m = _mk_material(sqlite_session, "Aluminum")
        _mk_reg(sqlite_session, "US_232_TARIFFS", geo="US", tar=0.6,
                material_id=m.id, scope_type="restricted")
        _conc, exp, tar, _sub, _method, diag = self._derive(
            sqlite_session, m.id, "US"
        )
        assert tar == 0.6 and exp == 0.0
        assert diag["tariff"]["standing_floor"]["regulation_key"] == "US_232_TARIFFS"
        assert diag["tariff"]["standing_floor"]["applied"] is True
