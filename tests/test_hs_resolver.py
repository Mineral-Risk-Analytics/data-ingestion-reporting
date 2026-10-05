"""Tests for app/services/ingestion/normalizers/hs_resolver.py.

Pure-function tests use real USGS Statistics_detail strings against
synthetic in-memory mappings; no DB fixtures required.  Module-level
behaviour (DB-backed candidate lookup, market_scope filter) is tested
via an in-memory SQLite session.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.models.supply import HsCodeMaterialMapping, Material
from app.services.ingestion.normalizers.hs_resolver import (
    _tokenize_description,
    resolve_hs_for_price_descriptor,
)


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture
def db():
    """In-memory SQLite session with the supply schema loaded.

    Tests insert their own Material + HsCodeMaterialMapping rows so each
    test starts from a known state.  Cheaper than a Postgres testcontainer
    for pure-function tests like this.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _make_material(db: Session, name: str) -> Material:
    m = Material(canonical_name=name, category="cathode_active")
    db.add(m)
    db.flush()
    return m


def _make_mapping(
    db: Session,
    material_id: int,
    hs_code: str,
    description: str,
    *,
    keywords: list[str] | None = None,
    stage: str = "battery_grade",
    digit_count: int = 6,
    confidence: float = 1.0,
    market_scope: str = "global",
) -> HsCodeMaterialMapping:
    m = HsCodeMaterialMapping(
        material_id=material_id,
        hs_code_prefix=hs_code,
        description=description,
        keywords=keywords,
        supply_chain_stage=stage,
        digit_count=digit_count,
        confidence=confidence,
        market_scope=market_scope,
    )
    db.add(m)
    db.flush()
    return m


# ── Tokenizer ────────────────────────────────────────────────────────────


class TestTokenizeDescription:
    def test_keeps_substantive_words(self):
        tokens = _tokenize_description("Lithium carbonate (Li₂CO₃) — battery-grade precursor")
        assert "lithium" in tokens
        assert "carbonate" in tokens
        assert "battery" in tokens
        assert "grade" in tokens

    def test_drops_short_words(self):
        # "Li" and "of" and "to" are below _MIN_DESC_WORD_LEN = 4
        tokens = _tokenize_description("Refined Cu of grade A")
        assert "li" not in tokens
        assert "of" not in tokens
        assert "to" not in tokens
        assert "refined" in tokens
        assert "grade" in tokens

    def test_drops_stopwords(self):
        # "price", "average", "annual" all in _STOPWORDS
        tokens = _tokenize_description("Price annual average dollars per metric ton")
        for stop in {"price", "annual", "average", "metric", "dollars"}:
            assert stop not in tokens

    def test_handles_empty_string(self):
        assert _tokenize_description("") == set()


# ── Resolver — happy path ────────────────────────────────────────────────


class TestResolveLithium:
    def test_battery_grade_carbonate_matches_specific_hs(self, db):
        li = _make_material(db, "Lithium")
        _make_mapping(
            db, li.id, "283691",
            "Lithium carbonate (Li2CO3) - battery-grade precursor",
            stage="battery_grade",
        )
        _make_mapping(
            db, li.id, "282520",
            "Lithium hydroxide LiOH refined",
            stage="battery_grade",
        )
        _make_mapping(
            db, li.id, "253090",
            "Spodumene concentrate other mineral substances",
            stage="concentrate",
        )

        hs_id, score, via = resolve_hs_for_price_descriptor(
            db, li.id,
            "Price, annual average-real, battery-grade lithium carbonate",
        )
        # 283691 should win: "lithium" + "carbonate" + "battery" + "grade"
        # all appear in both the USGS detail and the description.
        winning_row = db.get(HsCodeMaterialMapping, hs_id)
        assert winning_row.hs_code_prefix == "283691"
        assert score >= 4.0  # at least 4 description-word matches
        assert via == "description"

    def test_curated_keyword_beats_generic_description(self, db):
        li = _make_material(db, "Lithium")
        # 283691 has a curated keyword that maps US-spot battery-grade
        # carbonate; 282520 has only the generic description.
        _make_mapping(
            db, li.id, "283691",
            "Lithium carbonate",
            keywords=["battery-grade lithium carbonate"],  # exact-match curation
        )
        _make_mapping(db, li.id, "282520", "Lithium hydroxide")

        hs_id, score, via = resolve_hs_for_price_descriptor(
            db, li.id,
            "Price, average, battery-grade lithium carbonate",
        )
        winning_row = db.get(HsCodeMaterialMapping, hs_id)
        assert winning_row.hs_code_prefix == "283691"
        # 1 keyword (×2.0) + several description-word matches → total includes
        # the keyword bonus that pushed it past the floor confidently.
        assert score >= 2.0
        assert via in {"keywords", "mixed"}


# ── Resolver — material scoping ─────────────────────────────────────────


class TestPluralSingularMatching:
    """Regression tests for the stemming fix that handles English plurals.

    Before stemming was added, a description like "Cobalt cathodes" did
    not match a USGS descriptor saying "cathode" because the resolver did
    a one-way ``description_token in descriptor`` check.  The token
    ``"cathodes"`` is not a substring of ``"cathode"``, so the match
    failed even though semantically they refer to the same concept.
    """

    def test_description_plural_matches_descriptor_singular(self, db):
        co = _make_material(db, "Cobalt")
        _make_mapping(
            db, co.id, "810520",
            "Cobalt cathodes refined unwrought",   # plural
            stage="refined",
        )
        hs_id, score, via = resolve_hs_for_price_descriptor(
            db, co.id,
            "Price, dollars per pound, U.S. spot cobalt cathode",  # singular
        )
        winning_row = db.get(HsCodeMaterialMapping, hs_id)
        assert winning_row.hs_code_prefix == "810520"
        # "cobalt" + "cathode/cathodes" → both stem to common tokens
        assert score >= 2.0
        assert via == "description"

    def test_descriptor_plural_matches_description_singular(self, db):
        # Symmetric case: descriptor has plural, description has singular.
        ni = _make_material(db, "Nickel")
        _make_mapping(
            db, ni.id, "750120",
            "Nickel oxide sinter intermediate",   # singular
            stage="intermediate",
        )
        hs_id, score, via = resolve_hs_for_price_descriptor(
            db, ni.id,
            "Price, nickel oxide sinters",   # plural
        )
        assert hs_id is not None
        winning_row = db.get(HsCodeMaterialMapping, hs_id)
        assert winning_row.hs_code_prefix == "750120"


class TestResolverMaterialScoping:
    def test_does_not_cross_materials(self, db):
        # Both Cobalt and Nickel chapters mention "London Metal Exchange".
        # The resolver must scope to the asked material and never match
        # the wrong one even if the descriptor would otherwise fit.
        co = _make_material(db, "Cobalt")
        ni = _make_material(db, "Nickel")
        _make_mapping(
            db, co.id, "810520",
            "Cobalt cathodes unwrought refined London Metal Exchange grade",
            stage="refined",
        )
        _make_mapping(
            db, ni.id, "750110",
            "Nickel mattes sulfide intermediate London Metal Exchange grade",
            stage="intermediate",
        )

        # Cobalt LME → should land at 810520, never at 750110
        hs_id, _, _ = resolve_hs_for_price_descriptor(
            db, co.id,
            "Price, dollars per pound, London Metal Exchange (LME), cobalt cathode",
        )
        winning_row = db.get(HsCodeMaterialMapping, hs_id)
        assert winning_row.hs_code_prefix == "810520"


# ── Resolver — fallback behaviour ───────────────────────────────────────


class TestResolverFallback:
    def test_falls_back_to_none_below_threshold(self, db):
        # Material exists but no mapping's description overlaps strongly
        # enough with the descriptor.  Resolver returns None so caller
        # falls back to material-level pricing.
        ni = _make_material(db, "Nickel")
        _make_mapping(
            db, ni.id, "750110", "Nickel mattes intermediate",
        )

        hs_id, score, via = resolve_hs_for_price_descriptor(
            db, ni.id,
            "Price, generic spot quote",  # no overlap with descriptions
        )
        assert hs_id is None
        assert via is None
        # Score might be > 0 but below the 2.0 floor — that's the partner
        # signal to add a keyword if this benchmark matters.
        assert score < 2.0

    def test_no_candidates_returns_none(self, db):
        # No HS mapping rows for this material at all.  Returns immediately.
        ag = _make_material(db, "Silver")  # nothing seeded
        hs_id, score, via = resolve_hs_for_price_descriptor(
            db, ag.id, "Price, silver ingot, U.S. market",
        )
        assert hs_id is None
        assert score == 0.0
        assert via is None

    def test_empty_descriptor_returns_none(self, db):
        li = _make_material(db, "Lithium")
        _make_mapping(db, li.id, "283691", "Lithium carbonate")

        hs_id, score, via = resolve_hs_for_price_descriptor(db, li.id, "")
        assert hs_id is None and score == 0.0 and via is None

        hs_id, score, via = resolve_hs_for_price_descriptor(db, li.id, "    ")
        assert hs_id is None and score == 0.0 and via is None


# ── Resolver — market_scope filter ──────────────────────────────────────


class TestMarketScope:
    def test_default_global_only(self, db):
        # A US-scoped mapping must NOT match when caller asks for global.
        li = _make_material(db, "Lithium")
        _make_mapping(
            db, li.id, "283691",
            "Lithium carbonate battery-grade",
            market_scope="us",  # NOT global
        )
        hs_id, score, via = resolve_hs_for_price_descriptor(
            db, li.id, "battery-grade lithium carbonate",
        )
        # No global candidates → resolver returns None
        assert hs_id is None
        assert via is None

    def test_explicit_us_scope_works(self, db):
        li = _make_material(db, "Lithium")
        _make_mapping(
            db, li.id, "283691",
            "Lithium carbonate battery-grade",
            market_scope="us",
        )
        hs_id, _, _ = resolve_hs_for_price_descriptor(
            db, li.id, "battery-grade lithium carbonate",
            market_scope="us",
        )
        winning_row = db.get(HsCodeMaterialMapping, hs_id)
        assert winning_row.hs_code_prefix == "283691"
