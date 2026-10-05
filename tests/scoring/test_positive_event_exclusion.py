"""Positive-direction events never enter risk arithmetic (2026-07-28).

Nicole's call: risk is a STRUCTURAL measurement. A supportive policy does
not make sourcing from the DRC less risky — that requires WGI to move,
production share to disperse, or the material to come off a restriction
list. So ``POSITIVE_EVENT_SUBTYPES`` rows are dropped from every scoring
query rather than netted against the score. Design rationale and the
measured before-state: ``docs/design/positive_event_sign_vs_mitigation.md``.

Three things are pinned here, and only the first is obvious:

1. Every pillar-scoring query drops positives. Before this change only the
   geopolitical pillar filtered them (via its own subtype allowlist); the
   other four scored good news as risk.

2. **NULL-subtype events still score.** This is the regression that would
   be silent and catastrophic. SQL three-valued logic evaluates
   ``NULL NOT IN ('POSITIVE_POLICY', ...)`` to NULL, which FAILS a WHERE
   clause — so a bare ``not_in`` would drop every event with no subtype,
   which is most of the corpus (GTA interventions, POLICY_MILESTONE rows,
   all Federal Register notices). The scores would fall, plausibly, and
   nothing would look broken.

3. The evidence drawer still SHOWS positives. The filter is on the
   arithmetic, not on visibility — a partner reading a rationale should
   still see the supportive policy that landed that quarter.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

import pytest

from app.constants import POSITIVE_EVENT_SUBTYPES, RiskCategory
from app.models.company import Company
from app.models.regulatory import (
    Regulation,
    RiskEvent,
    RiskEventCompany,
    RiskEventGeography,
    RiskEventMaterial,
    RiskEventRegulation,
)
from app.models.supply import Material
from app.services.scoring.evidence_query import (
    get_events_for_company,
    get_events_for_geographies,
    get_events_for_material,
    get_events_for_materials,
    get_events_for_regulations,
    get_evidence_for_material_x_geography,
    get_filing_signals,
)

AS_OF = date(2026, 7, 1)
EVENT_DATE = datetime(2026, 6, 15, tzinfo=timezone.utc)
COUNTRY = "CD"


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _event(session, *, subtype, category, title="Event") -> RiskEvent:
    """One canonical, scoring-eligible event with an explicit subtype.

    ``primary_category`` is set explicitly rather than derived, mirroring
    the write paths that set it directly (triage promotion, the GTA
    ingester) — those bypass the before_insert autofill listener, which is
    exactly why the read-time gate is the enforcement point.
    """
    ev = RiskEvent(
        event_type="MANUAL",
        title=title,
        event_date=EVENT_DATE,
        severity_score=0.8,
        confidence_score=0.9,
        risk_categories_json=[category.value],
        primary_category=category.value,
        event_subtype=subtype,
    )
    session.add(ev)
    session.flush()
    return ev


def _anchor_material(session, event, material_id, *, is_direct=True) -> None:
    session.add(RiskEventMaterial(
        risk_event_id=event.id, material_id=material_id,
        relevance_score=1.0, is_direct=is_direct,
    ))


def _anchor_geography(session, event, country=COUNTRY) -> None:
    session.add(RiskEventGeography(
        risk_event_id=event.id, country_code=country,
        geography_context="primary", relevance_score=1.0,
    ))


@pytest.fixture()
def material(sqlite_session):
    m = Material(canonical_name="Cobalt")
    sqlite_session.add(m)
    sqlite_session.flush()
    return m


@pytest.fixture()
def company(sqlite_session):
    c = Company(id=uuid.uuid4(), canonical_name="Test Miner SA")
    sqlite_session.add(c)
    sqlite_session.flush()
    return c


# The three subtype cases every scoring query must handle identically.
#   negative  → scores (the control; proves the fixture reaches the query)
#   positive  → dropped
#   None      → scores (the three-valued-logic regression)
_CASES = [
    pytest.param("labor_strike", True, id="negative_subtype_scores"),
    pytest.param("POSITIVE_POLICY", False, id="positive_policy_dropped"),
    pytest.param("POSITIVE_DEVELOPMENT", False, id="positive_development_dropped"),
    pytest.param(None, True, id="null_subtype_still_scores"),
]


# ---------------------------------------------------------------------------
# 1. The constant itself
# ---------------------------------------------------------------------------

class TestPositiveSubtypeConstant:
    def test_contents(self):
        # Pinned deliberately: adding a subtype here removes events from
        # every risk score at once, so it should be a conscious diff.
        assert POSITIVE_EVENT_SUBTYPES == {"POSITIVE_POLICY", "POSITIVE_DEVELOPMENT"}

    def test_is_immutable(self):
        assert isinstance(POSITIVE_EVENT_SUBTYPES, frozenset)


# ---------------------------------------------------------------------------
# 2. Pillar-scoring queries
# ---------------------------------------------------------------------------

class TestMaterialAnchoredQueries:
    @pytest.mark.parametrize("subtype,should_score", _CASES)
    def test_get_events_for_material(self, sqlite_session, material, subtype, should_score):
        ev = _event(sqlite_session, subtype=subtype, category=RiskCategory.OPERATIONAL)
        _anchor_material(sqlite_session, ev, material.id)
        sqlite_session.commit()

        got = get_events_for_material(
            sqlite_session, material.id, RiskCategory.OPERATIONAL, AS_OF,
        )
        assert [e.event.id for e in got] == ([ev.id] if should_score else [])

    @pytest.mark.parametrize("subtype,should_score", _CASES)
    def test_get_events_for_materials(self, sqlite_session, material, subtype, should_score):
        ev = _event(sqlite_session, subtype=subtype, category=RiskCategory.OPERATIONAL)
        _anchor_material(sqlite_session, ev, material.id)
        sqlite_session.commit()

        got = get_events_for_materials(
            sqlite_session, {material.id}, RiskCategory.OPERATIONAL, AS_OF,
        )
        assert [e.event.id for e in got] == ([ev.id] if should_score else [])


class TestGeographyAnchoredQueries:
    @pytest.mark.parametrize("subtype,should_score", _CASES)
    def test_get_events_for_geographies(self, sqlite_session, subtype, should_score):
        ev = _event(sqlite_session, subtype=subtype, category=RiskCategory.OPERATIONAL)
        _anchor_geography(sqlite_session, ev)
        sqlite_session.commit()

        got = get_events_for_geographies(
            sqlite_session, {COUNTRY}, RiskCategory.OPERATIONAL, AS_OF,
        )
        assert [e.event.id for e in got] == ([ev.id] if should_score else [])

    def test_filter_is_not_category_specific(self, sqlite_session):
        """The gate applies to every pillar, not just operational.

        The pre-change bug was per-pillar divergence: geopolitical filtered
        positives, the other four did not. Parametrising over the category
        is the direct regression on that.
        """
        for category in RiskCategory:
            ev = _event(
                sqlite_session, subtype="POSITIVE_POLICY", category=category,
                title=f"Supportive measure ({category.value})",
            )
            _anchor_geography(sqlite_session, ev)
        sqlite_session.commit()

        for category in RiskCategory:
            got = get_events_for_geographies(
                sqlite_session, {COUNTRY}, category, AS_OF,
            )
            assert got == [], f"{category.value} still scores positives"


class TestCompanyAnchoredQueries:
    @pytest.mark.parametrize("subtype,should_score", _CASES)
    def test_get_events_for_company(self, sqlite_session, company, subtype, should_score):
        ev = _event(sqlite_session, subtype=subtype, category=RiskCategory.OPERATIONAL)
        sqlite_session.add(RiskEventCompany(
            risk_event_id=ev.id, company_id=company.id, relevance_score=1.0,
        ))
        sqlite_session.commit()

        got = get_events_for_company(
            sqlite_session, company.id, RiskCategory.OPERATIONAL, AS_OF,
        )
        assert [e.event.id for e in got] == ([ev.id] if should_score else [])

    @pytest.mark.parametrize("subtype,should_score", _CASES)
    def test_get_filing_signals(self, sqlite_session, company, subtype, should_score):
        """Financial pillar — the one where exclusion has a non-linear effect.

        ``score_financial_pressure`` scales a cell by
        ``evidence_count / 2.0`` when the count is below 2, so a single
        positive event surviving here doesn't just add its own impact — it
        can lift the sparse-evidence cap from 0.5x to 1.0x and double the
        cell's financial score.
        """
        ev = _event(
            sqlite_session, subtype=subtype,
            category=RiskCategory.FINANCIAL_PRESSURE,
        )
        sqlite_session.add(RiskEventCompany(
            risk_event_id=ev.id, company_id=company.id, relevance_score=1.0,
        ))
        sqlite_session.commit()

        got = get_filing_signals(sqlite_session, company.id)
        assert [e.event.id for e in got] == ([ev.id] if should_score else [])

    def test_filing_signal_count_excludes_positives(self, sqlite_session, company):
        """The evidence_count the sparse cap reads must not count good news."""
        for subtype in ("guidance_cut", "POSITIVE_DEVELOPMENT", "POSITIVE_POLICY"):
            ev = _event(
                sqlite_session, subtype=subtype,
                category=RiskCategory.FINANCIAL_PRESSURE, title=subtype,
            )
            sqlite_session.add(RiskEventCompany(
                risk_event_id=ev.id, company_id=company.id, relevance_score=1.0,
            ))
        sqlite_session.commit()

        got = get_filing_signals(sqlite_session, company.id)
        assert len(got) == 1
        assert got[0].event.event_subtype == "guidance_cut"


class TestRegulationAnchoredQueries:
    @pytest.mark.parametrize("subtype,should_score", _CASES)
    def test_get_events_for_regulations(self, sqlite_session, subtype, should_score):
        """Regulatory pillar — inert today, live the moment linking lands.

        As of 2026-07-28 none of the 12 positive regulatory events have a
        ``risk_event_regulations`` row, so this leak scores nothing yet.
        The structure is identical to operational's, so it activates as
        soon as event to regulation linking is backfilled.
        """
        reg = Regulation(
            regulation_key="TEST_REG",
            title="Test regulation",
            issuing_body="EU",
            geography="EU",
            policy_theme="supply_chain_resilience",
            status="effective",
            verified=True,
        )
        sqlite_session.add(reg)
        sqlite_session.flush()

        ev = _event(
            sqlite_session, subtype=subtype,
            category=RiskCategory.REGULATORY_COMPLIANCE,
        )
        sqlite_session.add(RiskEventRegulation(
            risk_event_id=ev.id, regulation_id=reg.id, relevance_score=1.0,
        ))
        sqlite_session.commit()

        got = get_events_for_regulations(sqlite_session, {"TEST_REG"}, AS_OF)
        assert [e.event.id for e in got] == ([ev.id] if should_score else [])


# ---------------------------------------------------------------------------
# 3. Display surface is deliberately NOT filtered
# ---------------------------------------------------------------------------

class TestEvidenceDrawerStillShowsPositives:
    def test_positive_visible_in_rationale_bundle(self, sqlite_session, material):
        """Excluded from the math, present in the evidence.

        If this test starts failing because someone added the gate to the
        display query "for consistency", that is the bug, not the fix:
        hiding the supportive policy makes the rationale panel misrepresent
        what actually happened in the period.
        """
        positive = _event(
            sqlite_session, subtype="POSITIVE_POLICY",
            category=RiskCategory.OPERATIONAL,
            title="Government backs refinery expansion",
        )
        negative = _event(
            sqlite_session, subtype="labor_strike",
            category=RiskCategory.OPERATIONAL,
            title="Strike halts output",
        )
        for ev in (positive, negative):
            _anchor_material(sqlite_session, ev, material.id)
            _anchor_geography(sqlite_session, ev)
        sqlite_session.commit()

        bundle = get_evidence_for_material_x_geography(
            sqlite_session, material.id, COUNTRY, as_of_date=AS_OF,
        )
        shown = {e.id for e in bundle.risk_events}
        assert positive.id in shown
        assert negative.id in shown
        assert bundle.risk_event_total == 2

        # ...and the same pair, through the scoring query, is one event.
        scored = get_events_for_material(
            sqlite_session, material.id, RiskCategory.OPERATIONAL, AS_OF,
        )
        assert [e.event.id for e in scored] == [negative.id]


# ---------------------------------------------------------------------------
# 4. Mixed corpus — the shape the operational pillar actually sees
# ---------------------------------------------------------------------------

class TestOperationalTopThreeRecompute:
    def test_positives_do_not_occupy_top_slots(self, sqlite_session, material):
        """``_score_operational_market`` takes the top-3 mean of impacts.

        Measured on live data 2026-07-28: 29 of 90 top-3 slots across the
        50 (material x country) cells with operational events were held by
        a positive, and 12 cells scored ENTIRELY from good news (up to 37
        operational points). The 4.4 top-3-mean change made thin cells
        worse, not better — with one event in a cell, that event IS the
        score.

        This asserts at the query layer, which is where the aggregator
        gets its list: the high-severity positives must not be in it.
        """
        high_positive = _event(
            sqlite_session, subtype="POSITIVE_POLICY",
            category=RiskCategory.OPERATIONAL,
            title="Major state financing package",
        )
        high_positive.severity_score = 0.95
        second_positive = _event(
            sqlite_session, subtype="POSITIVE_DEVELOPMENT",
            category=RiskCategory.OPERATIONAL,
            title="Permit granted ahead of schedule",
        )
        second_positive.severity_score = 0.90
        mild_negative = _event(
            sqlite_session, subtype="project_schedule_slip",
            category=RiskCategory.OPERATIONAL,
            title="Ramp-up slips a quarter",
        )
        mild_negative.severity_score = 0.30
        untyped = _event(
            sqlite_session, subtype=None,
            category=RiskCategory.OPERATIONAL,
            title="GTA intervention, no subtype",
        )
        untyped.severity_score = 0.50

        for ev in (high_positive, second_positive, mild_negative, untyped):
            _anchor_material(sqlite_session, ev, material.id)
        sqlite_session.commit()

        got = get_events_for_material(
            sqlite_session, material.id, RiskCategory.OPERATIONAL, AS_OF,
        )
        assert {e.event.id for e in got} == {mild_negative.id, untyped.id}
        # Without the gate this cell scored off two 0.9+ positives; with it,
        # the cell is honestly thin rather than falsely high.
        assert len(got) == 2
