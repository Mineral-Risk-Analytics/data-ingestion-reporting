"""Migration 067 piece 1 — Regulation pillar + standing-floor fields.

Model-level guarantees only (docs/design/regulation_pillar_reassignment.md
§3.1). The loader vocabulary/range enforcement is piece 2 and the
aggregator floor term is piece 3; until those land, the contract tested
here is: the columns exist, the vocabulary is closed, floors are pinned to
[0,1], and a row that sets none of them is exactly as legal as it was
before 067 (the no-behavior-change guarantee).
"""

from datetime import date

import pytest

from app.models.regulatory import Regulation


def _reg(**kwargs) -> Regulation:
    return Regulation(regulation_key="TEST_REG_067", **kwargs)


class TestPillarVocabulary:
    def test_accepts_the_three_values(self):
        for value in ("regulatory_compliance", "geopolitical_trade", "dual"):
            reg = _reg(pillar=value)
            assert reg.pillar == value

    def test_accepts_null(self):
        reg = _reg(pillar=None)
        assert reg.pillar is None

    def test_rejects_unknown_value(self):
        with pytest.raises(ValueError, match="pillar must be one of"):
            _reg(pillar="geopolitical")  # near-miss spelling must not pass

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="pillar must be one of"):
            _reg(pillar="")


class TestStandingFloorRange:
    def test_accepts_bounds_and_interior(self):
        reg = _reg(standing_export_restriction=0.0, standing_tariff_exposure=1.0)
        assert reg.standing_export_restriction == 0.0
        assert reg.standing_tariff_exposure == 1.0
        reg2 = _reg(standing_export_restriction=0.55)  # DRC quota, evidence memo
        assert reg2.standing_export_restriction == 0.55

    def test_accepts_null(self):
        reg = _reg(
            standing_export_restriction=None, standing_tariff_exposure=None
        )
        assert reg.standing_export_restriction is None
        assert reg.standing_tariff_exposure is None

    def test_rejects_above_one(self):
        with pytest.raises(ValueError, match="standing_export_restriction"):
            _reg(standing_export_restriction=1.2)

    def test_rejects_negative(self):
        with pytest.raises(ValueError, match="standing_tariff_exposure"):
            _reg(standing_tariff_exposure=-0.1)


class TestNoBehaviorChangeDefaults:
    def test_bare_row_stays_legal_with_all_067_fields_unset(self):
        """A pre-067-shaped row — nothing set — must construct exactly as
        before: pillar/floors/review date all default to None."""
        reg = _reg()
        assert reg.pillar is None
        assert reg.standing_export_restriction is None
        assert reg.standing_tariff_exposure is None
        assert reg.floor_review_date is None

    def test_review_date_is_plain_data(self):
        """floor_review_date carries no validation — perishability is a
        workbook chore, not a code-enforced gate (design §3.1)."""
        reg = _reg(floor_review_date=date(2026, 11, 27))
        assert reg.floor_review_date == date(2026, 11, 27)
