"""Tests for chemistry-mix queries and chemistry-aware material weighting.

Two layers under test:

1. ``get_chemistry_mix_for_company`` — volume-weighted aggregation across
   active models, time-window filtered, normalised to 1.0, ``None`` when no
   models exist.
2. ``derive_material_inputs`` — when chemistry mix + intensities are passed,
   each ``CompanyMaterialExposure`` is re-weighted by its material's
   chemistry-aware intensity (so a 100% LFP OEM weights cobalt near zero and
   Li/Fe/P near full strength).
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models.battery_chemistry import (
    BatteryChemistry,
    BatteryChemistryMaterial,
)
from app.models.company import Company
from app.models.supply import Material
from app.models.vehicle import CompanyVehicleModel, VehicleModelChemistry
from app.services.scoring.evidence_aggregator import (
    _CHEMISTRY_BASELINE_UNMATCHED,
    derive_material_inputs,
)
from app.services.scoring.evidence_query import (
    get_chemistry_material_intensities,
    get_chemistry_mix_for_company,
)
from app.services.scoring.types import ScoringScope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_company(s, name: str) -> Company:
    c = Company(canonical_name=name, headquarters_country="US")
    s.add(c); s.flush()
    return c


def _make_chemistry(s, slug: str, name: str | None = None) -> BatteryChemistry:
    chem = BatteryChemistry(
        slug=slug,
        name=name or slug.upper(),
        status="commercial",
    )
    s.add(chem); s.flush()
    return chem


def _make_material(s, name: str) -> Material:
    m = Material(canonical_name=name)
    s.add(m); s.flush()
    return m


def _make_model(
    s,
    company: Company,
    name: str,
    *,
    volume: int | None,
    chemistries: list[tuple[BatteryChemistry, float, date, date | None]],
) -> CompanyVehicleModel:
    model = CompanyVehicleModel(
        company_id=company.id,
        model_name=name,
        production_volume_units=volume,
        is_active=True,
    )
    s.add(model); s.flush()
    for chem, share, vfrom, vto in chemistries:
        s.add(
            VehicleModelChemistry(
                vehicle_model_id=model.id,
                battery_chemistry_id=chem.id,
                share_pct=share,
                valid_from=vfrom,
                valid_to=vto,
            )
        )
    s.flush()
    return model


def _exposure(material_id: int, country: str | None = "CN", score: float = 0.5):
    e = MagicMock()
    e.material_id = material_id
    e.source_geography = country
    e.exposure_score = score
    return e


# ---------------------------------------------------------------------------
# get_chemistry_mix_for_company
# ---------------------------------------------------------------------------

class TestGetChemistryMixForCompany:
    def test_no_models_returns_none(self, sqlite_session):
        c = _make_company(sqlite_session, "OEM")
        assert get_chemistry_mix_for_company(sqlite_session, c.id) is None

    def test_single_model_pure_chem_returns_unit_share(self, sqlite_session):
        c = _make_company(sqlite_session, "PureLFP")
        lfp = _make_chemistry(sqlite_session, "lfp")
        _make_model(
            sqlite_session,
            c,
            "Model A",
            volume=1000,
            chemistries=[(lfp, 1.0, date(2024, 1, 1), None)],
        )
        out = get_chemistry_mix_for_company(sqlite_session, c.id)
        assert out == {lfp.id: pytest.approx(1.0)}

    def test_two_models_volume_weighted(self, sqlite_session):
        c = _make_company(sqlite_session, "MixedOEM")
        lfp = _make_chemistry(sqlite_session, "lfp")
        nmc = _make_chemistry(sqlite_session, "nmc")
        _make_model(sqlite_session, c, "M1", volume=300,
                    chemistries=[(lfp, 1.0, date(2024, 1, 1), None)])
        _make_model(sqlite_session, c, "M2", volume=700,
                    chemistries=[(nmc, 1.0, date(2024, 1, 1), None)])

        out = get_chemistry_mix_for_company(sqlite_session, c.id)
        assert out[lfp.id] == pytest.approx(0.3)
        assert out[nmc.id] == pytest.approx(0.7)
        assert sum(out.values()) == pytest.approx(1.0)

    def test_null_volume_falls_back_to_unit_weight(self, sqlite_session):
        c = _make_company(sqlite_session, "NoVolOEM")
        lfp = _make_chemistry(sqlite_session, "lfp")
        nmc = _make_chemistry(sqlite_session, "nmc")
        _make_model(sqlite_session, c, "M1", volume=None,
                    chemistries=[(lfp, 1.0, date(2024, 1, 1), None)])
        _make_model(sqlite_session, c, "M2", volume=None,
                    chemistries=[(nmc, 1.0, date(2024, 1, 1), None)])
        out = get_chemistry_mix_for_company(sqlite_session, c.id)
        assert out[lfp.id] == pytest.approx(0.5)
        assert out[nmc.id] == pytest.approx(0.5)

    def test_chemistry_outside_validity_window_excluded(self, sqlite_session):
        c = _make_company(sqlite_session, "WindowOEM")
        lfp = _make_chemistry(sqlite_session, "lfp")
        nmc = _make_chemistry(sqlite_session, "nmc")
        # NMC is the OLD chemistry (closed window), LFP is the new one.
        _make_model(
            sqlite_session,
            c,
            "M1",
            volume=100,
            chemistries=[
                (nmc, 1.0, date(2020, 1, 1), date(2024, 1, 1)),
                (lfp, 1.0, date(2024, 1, 1), None),
            ],
        )
        out = get_chemistry_mix_for_company(
            sqlite_session, c.id, as_of_date=date(2025, 6, 1)
        )
        assert out == {lfp.id: pytest.approx(1.0)}

    def test_scope_chemistry_ids_filter(self, sqlite_session):
        c = _make_company(sqlite_session, "ScopedOEM")
        lfp = _make_chemistry(sqlite_session, "lfp")
        nmc = _make_chemistry(sqlite_session, "nmc")
        _make_model(sqlite_session, c, "M1", volume=500,
                    chemistries=[(lfp, 1.0, date(2024, 1, 1), None)])
        _make_model(sqlite_session, c, "M2", volume=500,
                    chemistries=[(nmc, 1.0, date(2024, 1, 1), None)])

        out = get_chemistry_mix_for_company(
            sqlite_session,
            c.id,
            scope=ScoringScope(chemistry_ids=frozenset({lfp.id})),
        )
        # Scope re-normalises to the visible chemistries.
        assert out == {lfp.id: pytest.approx(1.0)}


# ---------------------------------------------------------------------------
# get_chemistry_material_intensities
# ---------------------------------------------------------------------------

class TestGetChemistryMaterialIntensities:
    def test_returns_intensities_within_window(self, sqlite_session):
        lfp = _make_chemistry(sqlite_session, "lfp")
        li = _make_material(sqlite_session, "Lithium-x")
        sqlite_session.add(
            BatteryChemistryMaterial(
                battery_chemistry_id=lfp.id,
                material_id=li.id,
                role="cathode_active",
                intensity=0.07,
                valid_from=date(2024, 1, 1),
            )
        )
        sqlite_session.flush()

        out = get_chemistry_material_intensities(
            sqlite_session, {lfp.id}, as_of_date=date(2025, 6, 1)
        )
        assert out == {lfp.id: {li.id: pytest.approx(0.07)}}

    def test_excludes_rows_outside_validity_window(self, sqlite_session):
        nmc = _make_chemistry(sqlite_session, "nmc")
        ni = _make_material(sqlite_session, "Nickel-x")
        sqlite_session.add_all([
            BatteryChemistryMaterial(
                battery_chemistry_id=nmc.id, material_id=ni.id,
                role="cathode_active", intensity=0.60,
                valid_from=date(2020, 1, 1), valid_to=date(2024, 1, 1),
            ),
            BatteryChemistryMaterial(
                battery_chemistry_id=nmc.id, material_id=ni.id,
                role="cathode_active", intensity=0.80,
                valid_from=date(2024, 1, 1),
            ),
        ])
        sqlite_session.flush()

        out = get_chemistry_material_intensities(
            sqlite_session, {nmc.id}, as_of_date=date(2025, 6, 1)
        )
        assert out[nmc.id][ni.id] == pytest.approx(0.80)

    def test_empty_input(self, sqlite_session):
        assert get_chemistry_material_intensities(sqlite_session, set()) == {}


# ---------------------------------------------------------------------------
# derive_material_inputs — chemistry-aware re-weighting
# ---------------------------------------------------------------------------

class TestChemistryAwareMaterialWeighting:
    def test_unmatched_material_uses_baseline(self):
        """A material not used by any active chemistry must still contribute
        — but at the ``_CHEMISTRY_BASELINE_UNMATCHED`` floor, not zero."""
        crit, _, _ = derive_material_inputs(
            material_exposures=[_exposure(material_id=99, score=1.0)],
            trade_events=[],
            as_of_date=date(2025, 6, 1),
            chemistry_mix={1: 1.0},          # 100% chemistry id 1
            chemistry_intensities={1: {}},   # no materials → unmatched
        )
        assert crit == pytest.approx(1.0)  # only one exposure, weight cancels

    def test_high_intensity_material_dominates_low(self):
        """100% LFP: lithium intensity 0.07, cobalt intensity 0 → cobalt is
        unmatched (baseline 0.10), lithium is full-weight. The
        criticality reflects the lithium exposure score far more than cobalt."""
        chem_mix = {1: 1.0}  # 100% LFP
        intensities = {1: {10: 0.07}}  # only material 10 is in LFP recipe
        # exposure scores are intentionally distinct so the weighting shows up.
        crit_no_chem, _, _ = derive_material_inputs(
            material_exposures=[
                _exposure(material_id=10, score=0.20),  # lithium, low risk
                _exposure(material_id=20, score=1.00),  # cobalt, high risk
            ],
            trade_events=[],
            as_of_date=date(2025, 6, 1),
        )
        crit_with_chem, _, _ = derive_material_inputs(
            material_exposures=[
                _exposure(material_id=10, score=0.20),
                _exposure(material_id=20, score=1.00),
            ],
            trade_events=[],
            as_of_date=date(2025, 6, 1),
            chemistry_mix=chem_mix,
            chemistry_intensities=intensities,
        )
        # No chemistry: simple average → 0.6
        assert crit_no_chem == pytest.approx(0.6)
        # With chemistry: lithium contributes via its actual intensity (0.07)
        # while cobalt is unmatched and falls to the baseline floor (0.10).
        # Both weights are small but the formula must match exactly.
        expected = (0.20 * 0.07 + 1.00 * _CHEMISTRY_BASELINE_UNMATCHED) / (
            0.07 + _CHEMISTRY_BASELINE_UNMATCHED
        )
        assert crit_with_chem == pytest.approx(expected)
        # Adding a chemistry mix changes the result vs uniform averaging.
        assert crit_with_chem != pytest.approx(crit_no_chem)

    def test_chemistry_concentration_reflects_chem_weight(self):
        """concentration HCG share is also chemistry-weighted: a high-Ni OEM's
        nickel exposures count more than its trace cobalt exposure."""
        chem_mix = {1: 1.0}
        intensities = {1: {10: 0.80, 20: 0.05}}  # mat10 dominant, mat20 trace

        _, conc, _ = derive_material_inputs(
            material_exposures=[
                _exposure(material_id=10, country="US", score=0.5),  # not HCG
                _exposure(material_id=20, country="CN", score=0.5),  # HCG
            ],
            trade_events=[],
            as_of_date=date(2025, 6, 1),
            chemistry_mix=chem_mix,
            chemistry_intensities=intensities,
        )
        # mat10 (US) weight 0.80 vs mat20 (CN) weight 0.05 → mostly non-HCG
        # → concentration should be small.
        expected = 0.05 / (0.80 + 0.05)
        assert conc == pytest.approx(expected)
