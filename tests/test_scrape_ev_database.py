"""Tests for app/services/ingestion/scrape_ev_database.py.

In-memory SQLite is used to exercise the upsert path end-to-end. HTML
fixtures live in ``tests/fixtures/ev_database/`` and cover the shapes the
parser cares about:

  detail_nmc.html          - real-style ``<td>Cathode Material</td><td>NMC</td>``
                             with title ``(YYYY-YYYY)`` year window.
  detail_lfp.html          - same Cathode Material layout, single LFP, also a
                             "Discontinued (Month YYYY - Month YYYY)" block.
  detail_dual.html         - legacy/inline ``Battery Chemistry: LFP & NMC``
                             pattern (covers the inline-text fallback).
  detail_no_chemistry.html - real-style "Cathode Material: No Data" cell that
                             must be treated as missing (not as the literal
                             string).
  real_nmc.html            - actual ev-database HTML for the BMW iX xDrive60
                             (full page, kept tiny by repo standards but
                             un-edited so we don't drift from production).
  real_nodata.html         - actual ev-database HTML for a Lucid variant whose
                             cathode is "No Data".
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Optional

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  — register all ORM tables before create_all
from app.db.base import Base
from app.models.battery_chemistry import BatteryChemistry
from app.models.company import Company, CompanyAlias
from app.models.vehicle import CompanyVehicleModel, VehicleModelChemistry
from app.services.ingestion import scrape_ev_database as mod
from app.services.ingestion.scrape_ev_database import (
    BASE_URL,
    DISCOVERY_PATH,
    ParsedVariant,
    RateLimitedError,
    Variant,
    _already_stored_car_ids,
    _brand_prefix,
    _filter_brands,
    _get_with_backoff,
    _parse_retry_after,
    chemistry_to_share_rows,
    discover_variants,
    parse_variant_detail,
    resolve_brand,
    run,
    upsert_variant,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "ev_database"


def _patch_sqlite_jsonb() -> None:
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler  # type: ignore[import]

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = SQLiteTypeCompiler.visit_JSON  # type: ignore[attr-defined]


@pytest.fixture()
def session() -> Session:
    _patch_sqlite_jsonb()
    engine = create_engine("sqlite:///:memory:", echo=False)
    Base.metadata.create_all(engine)
    Session_ = sessionmaker(bind=engine)
    s = Session_()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


def _seed_companies(session: Session) -> dict[str, Company]:
    companies = {
        "Tesla": Company(canonical_name="Tesla"),
        "Volkswagen Group": Company(canonical_name="Volkswagen Group"),
        "Mercedes-Benz Group": Company(canonical_name="Mercedes-Benz Group"),
        "BMW Group": Company(canonical_name="BMW Group"),
    }
    for c in companies.values():
        session.add(c)
    session.flush()
    return companies


def _seed_chemistries(session: Session) -> dict[str, BatteryChemistry]:
    chems = {
        "nmc": BatteryChemistry(slug="nmc", name="NMC", status="commercial"),
        "lfp": BatteryChemistry(slug="lfp", name="LFP", status="commercial"),
        "nca": BatteryChemistry(slug="nca", name="NCA", status="commercial"),
    }
    for c in chems.values():
        session.add(c)
    session.flush()
    return chems


def _read(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# parse_variant_detail
# ---------------------------------------------------------------------------


class TestParseVariantDetail:
    def test_cathode_material_table_yields_nmc(self):
        parsed = parse_variant_detail(_read("detail_nmc.html"))
        assert parsed.chemistry_raw == "NMC"
        assert parsed.useable_battery_kwh == pytest.approx(108.9)
        assert parsed.model_year_start == 2025
        assert parsed.model_year_end == 2026
        assert parsed.is_discontinued is False

    def test_lfp_with_year_range_and_discontinued_block(self):
        parsed = parse_variant_detail(_read("detail_lfp.html"))
        assert parsed.chemistry_raw == "LFP"
        assert parsed.useable_battery_kwh == pytest.approx(57.0)
        assert parsed.model_year_start == 2022
        assert parsed.model_year_end == 2025
        assert parsed.is_discontinued is True

    def test_dual_chemistry_inline_pattern(self):
        parsed = parse_variant_detail(_read("detail_dual.html"))
        assert parsed.chemistry_raw is not None
        assert "LFP" in parsed.chemistry_raw and "NMC" in parsed.chemistry_raw
        assert parsed.useable_battery_kwh == pytest.approx(118.0)
        assert parsed.model_year_start == 2024
        assert parsed.model_year_end == 2026

    def test_cathode_material_no_data_treated_as_missing(self):
        parsed = parse_variant_detail(_read("detail_no_chemistry.html"))
        assert parsed.chemistry_raw is None
        assert parsed.useable_battery_kwh == pytest.approx(80.0)
        assert parsed.model_year_start == 2027

    def test_real_page_with_cathode_material_nmc(self):
        # Un-edited ev-database HTML; guards against a future site change
        # silently breaking the parser.
        parsed = parse_variant_detail(_read("real_nmc.html"))
        assert parsed.chemistry_raw == "NMC"
        assert parsed.model_year_start == 2025
        assert parsed.model_year_end == 2026
        assert parsed.useable_battery_kwh == pytest.approx(109.1)

    def test_real_page_no_data_returns_none(self):
        parsed = parse_variant_detail(_read("real_nodata.html"))
        assert parsed.chemistry_raw is None
        # MY25-26 takes priority over the (2024-2026) plain title range.
        assert parsed.model_year_start == 2025
        assert parsed.model_year_end == 2026


# ---------------------------------------------------------------------------
# chemistry_to_share_rows
# ---------------------------------------------------------------------------


class TestChemistryToShareRows:
    valid_from = date(2025, 1, 1)

    def test_single_nmc_yields_full_share(self):
        rows = chemistry_to_share_rows("NMC", self.valid_from)
        assert rows == [("nmc", 1.0, self.valid_from)]

    def test_lowercase_input_resolves(self):
        rows = chemistry_to_share_rows("lfp", self.valid_from)
        assert rows == [("lfp", 1.0, self.valid_from)]

    def test_dual_chemistry_ampersand_splits_evenly(self):
        rows = chemistry_to_share_rows("LFP & NMC", self.valid_from)
        assert rows == [("lfp", 0.5, self.valid_from), ("nmc", 0.5, self.valid_from)]

    def test_slash_separator_also_splits(self):
        rows = chemistry_to_share_rows("NMC / LFP", self.valid_from)
        assert rows == [("nmc", 0.5, self.valid_from), ("lfp", 0.5, self.valid_from)]

    def test_unknown_token_yields_empty(self):
        assert chemistry_to_share_rows("Unobtainium", self.valid_from) == []

    def test_none_yields_empty(self):
        assert chemistry_to_share_rows(None, self.valid_from) == []

    def test_lmfp_alias_maps_to_lfmp(self):
        rows = chemistry_to_share_rows("LMFP", self.valid_from)
        assert rows == [("lfmp", 1.0, self.valid_from)]

    @pytest.mark.parametrize("token", ["NMC811", "NMC622", "NMC532", "NMC111", "NMC 811", "NMC-811", "NMC9.5.5"])
    def test_nmc_subtypes_collapse_to_nmc(self, token):
        rows = chemistry_to_share_rows(token, self.valid_from)
        assert rows == [("nmc", 1.0, self.valid_from)]

    def test_dual_subtypes_dedupe_to_single_slug(self):
        # NMC811 + NMC622 are both "nmc" — share collapses to a single 1.0 row.
        rows = chemistry_to_share_rows("NMC811 & NMC622", self.valid_from)
        assert rows == [("nmc", 1.0, self.valid_from)]

    def test_nmca_maps_to_nmc(self):
        rows = chemistry_to_share_rows("NMCA", self.valid_from)
        assert rows == [("nmc", 1.0, self.valid_from)]


# ---------------------------------------------------------------------------
# brand prefix + resolve_brand
# ---------------------------------------------------------------------------


class TestBrandResolution:
    def test_prefix_picks_longest_match(self):
        # "Mercedes-Benz" must beat "Mercedes" since both are mapped.
        assert _brand_prefix("Mercedes-Benz EQS 450+") == "Mercedes-Benz"

    def test_prefix_returns_none_for_unknown(self):
        assert _brand_prefix("Acme Phantom EV") is None

    def test_resolve_tesla_direct(self, session: Session):
        _seed_companies(session)
        company = resolve_brand(session, "Tesla Model Y RWD (CATL LFP)")
        assert company is not None
        assert company.canonical_name == "Tesla"

    def test_resolve_audi_via_brand_map(self, session: Session):
        _seed_companies(session)
        company = resolve_brand(session, "Audi e-tron GT quattro")
        assert company is not None
        assert company.canonical_name == "Volkswagen Group"

    def test_resolve_unknown_returns_none(self, session: Session):
        _seed_companies(session)
        assert resolve_brand(session, "Acme Phantom EV") is None

    def test_resolve_via_alias_fallback(self, session: Session):
        # Brand isn't in BRAND_TO_CANONICAL but has an alias row.
        rivian = Company(canonical_name="Rivian Automotive")
        session.add(rivian)
        session.flush()
        session.add(CompanyAlias(company_id=rivian.id, alias="Rivian", alias_type="aka"))
        session.flush()
        company = resolve_brand(session, "Rivian R1T")
        assert company is not None
        assert company.canonical_name == "Rivian Automotive"

    def test_resolve_brand_mapped_but_company_missing(self, session: Session):
        # Map says "Tesla", but Tesla isn't seeded.
        assert resolve_brand(session, "Tesla Model 3") is None


# ---------------------------------------------------------------------------
# discover_variants
# ---------------------------------------------------------------------------


class TestDiscoverVariants:
    def test_dedups_by_car_id_and_returns_sorted(self):
        cheatsheet = _read("cheatsheet_range.html")

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == DISCOVERY_PATH
            return httpx.Response(200, text=cheatsheet)

        client = httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url=BASE_URL,
        )
        try:
            variants = discover_variants(client)
        finally:
            client.close()

        # Five unique IDs from the fixture (Lucid duplicated once).
        assert len(variants) == 5
        names = [v.display_name for v in variants]
        assert names == sorted(names, key=str.lower)
        ids = {v.car_id for v in variants}
        assert ids == {2049, 3273, 2193, 3110, 9999}
        # URL is built from the harvested id + slug.
        lucid = next(v for v in variants if v.car_id == 3273)
        assert lucid.url == f"{BASE_URL}/car/3273/Lucid-Air-Grand-Touring"


# ---------------------------------------------------------------------------
# upsert_variant — idempotency
# ---------------------------------------------------------------------------


class TestUpsertVariant:
    def _setup(self, session: Session):
        companies = _seed_companies(session)
        chems = _seed_chemistries(session)
        chemistry_id_by_slug = {c.slug: c.id for c in chems.values()}
        return companies, chemistry_id_by_slug

    def test_inserts_new_model_and_chemistry(self, session: Session):
        companies, chem_ids = self._setup(session)
        variant = Variant(display_name="BMW iX xDrive60", car_id=3110, slug="BMW-iX-xDrive60")
        parsed = ParsedVariant(
            model_year_start=2026,
            model_year_end=None,
            useable_battery_kwh=108.9,
            chemistry_raw="NMC",
        )
        chem_rows = chemistry_to_share_rows("NMC", date(2026, 1, 1))

        model, changed, written = upsert_variant(
            session,
            company_id=companies["BMW Group"].id,
            variant=variant,
            parsed=parsed,
            chem_rows=chem_rows,
            chemistry_id_by_slug=chem_ids,
        )
        assert changed is True
        assert written == 1
        assert model.id is not None
        assert model.production_volume_units is None
        assert model.data_source == "ev-database.org"
        assert model.metadata_json["ev_database_id"] == 3110

        # Rerun with identical input → no change, no chem rows written.
        _, changed2, written2 = upsert_variant(
            session,
            company_id=companies["BMW Group"].id,
            variant=variant,
            parsed=parsed,
            chem_rows=chem_rows,
            chemistry_id_by_slug=chem_ids,
        )
        assert changed2 is False
        assert written2 == 0
        # Still exactly one chemistry row.
        all_chem = session.scalars(
            select(VehicleModelChemistry).where(
                VehicleModelChemistry.vehicle_model_id == model.id
            )
        ).all()
        assert len(all_chem) == 1

    def test_chemistry_change_replaces_rows_keeps_model_id(self, session: Session):
        companies, chem_ids = self._setup(session)
        variant = Variant(display_name="BMW iX xDrive60", car_id=3110, slug="BMW-iX-xDrive60")
        parsed = ParsedVariant(model_year_start=2026, useable_battery_kwh=108.9, chemistry_raw="NMC")
        first_rows = chemistry_to_share_rows("NMC", date(2026, 1, 1))
        model_v1, _, _ = upsert_variant(
            session,
            company_id=companies["BMW Group"].id,
            variant=variant,
            parsed=parsed,
            chem_rows=first_rows,
            chemistry_id_by_slug=chem_ids,
        )
        original_id = model_v1.id

        # Now the source switches to LFP & NMC mid-cycle.
        parsed2 = ParsedVariant(
            model_year_start=2026, useable_battery_kwh=108.9, chemistry_raw="LFP & NMC"
        )
        second_rows = chemistry_to_share_rows("LFP & NMC", date(2026, 1, 1))
        model_v2, changed, written = upsert_variant(
            session,
            company_id=companies["BMW Group"].id,
            variant=variant,
            parsed=parsed2,
            chem_rows=second_rows,
            chemistry_id_by_slug=chem_ids,
        )
        assert model_v2.id == original_id
        assert changed is True
        assert written == 2
        rows = session.scalars(
            select(VehicleModelChemistry).where(
                VehicleModelChemistry.vehicle_model_id == original_id
            )
        ).all()
        assert len(rows) == 2
        assert {r.share_pct for r in rows} == {0.5}

    def test_unseeded_chemistry_slug_is_skipped_with_warning(self, session: Session):
        companies, chem_ids = self._setup(session)
        chem_ids.pop("lfp")  # simulate a missing seeded chemistry
        variant = Variant(display_name="Tesla Model Y RWD", car_id=2049, slug="Tesla-Model-Y-RWD")
        parsed = ParsedVariant(model_year_start=2024, chemistry_raw="LFP")
        rows = chemistry_to_share_rows("LFP", date(2024, 1, 1))
        model, changed, written = upsert_variant(
            session,
            company_id=companies["Tesla"].id,
            variant=variant,
            parsed=parsed,
            chem_rows=rows,
            chemistry_id_by_slug=chem_ids,
        )
        assert changed is True  # model itself was inserted
        assert written == 0  # but no chemistry row could be written


# ---------------------------------------------------------------------------
# _filter_brands
# ---------------------------------------------------------------------------


class TestFilterBrands:
    def _vs(self) -> list[Variant]:
        return [
            Variant("Tesla Model 3", 1, "Tesla-Model-3"),
            Variant("BMW iX xDrive60", 2, "BMW-iX-xDrive60"),
            Variant("Audi e-tron GT", 3, "Audi-e-tron-GT"),
            Variant("Acme Phantom EV", 9, "Acme-Phantom-EV"),
        ]

    def test_none_returns_all(self):
        assert len(_filter_brands(self._vs(), None)) == 4

    def test_empty_string_brand_returns_all(self):
        assert len(_filter_brands(self._vs(), [""])) == 4

    def test_filters_by_known_prefix(self):
        out = _filter_brands(self._vs(), ["Tesla", "BMW"])
        assert {v.display_name for v in out} == {"Tesla Model 3", "BMW iX xDrive60"}

    def test_filters_by_unknown_prefix_uses_first_token(self):
        # 'Acme' isn't in BRAND_TO_CANONICAL; falls back to first-token compare.
        out = _filter_brands(self._vs(), ["Acme"])
        assert [v.display_name for v in out] == ["Acme Phantom EV"]


# ---------------------------------------------------------------------------
# run() — end-to-end with mocked transport
# ---------------------------------------------------------------------------


def _build_mock_transport() -> httpx.MockTransport:
    cheatsheet = _read("cheatsheet_range.html")
    bodies: dict[str, str] = {
        DISCOVERY_PATH: cheatsheet,
        "/car/2049/Tesla-Model-Y-RWD": _read("detail_lfp.html"),
        "/car/3110/BMW-iX-xDrive60": _read("detail_nmc.html"),
        "/car/2193/Mercedes-Benz-EQS-450plus": _read("detail_dual.html"),
        "/car/9999/Acme-Phantom-EV": _read("detail_no_chemistry.html"),
        "/car/3273/Lucid-Air-Grand-Touring": _read("detail_nmc.html"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = bodies.get(request.url.path)
        if body is None:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


class TestRun:
    def test_full_run_skips_unmatched_brands_and_inserts_matches(
        self, session: Session
    ):
        _seed_companies(session)
        _seed_chemistries(session)

        client = httpx.Client(transport=_build_mock_transport(), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                client=client,
            )
        finally:
            client.close()

        # Discovery: 5 unique variants. 4 mapped brands + 1 unmapped (Acme) +
        # 1 mapped-but-no-company (Lucid Group not seeded).
        assert stats["discovered"] == 5
        # Acme has no brand mapping AND no alias → skipped.
        # Lucid Group is in BRAND_TO_CANONICAL but no Company seeded → skipped.
        assert stats["brand_skipped"] == 2
        assert stats["models_upserted"] == 3  # Tesla, BMW, Mercedes
        # NMC=1, LFP=1, LFP&NMC=2.
        assert stats["chem_rows_upserted"] == 4

        models = session.scalars(select(CompanyVehicleModel)).all()
        assert len(models) == 3
        names = {m.model_name for m in models}
        assert "Tesla Model Y RWD (CATL LFP)" in names
        assert "BMW iX xDrive60" in names
        assert "Mercedes-Benz EQS 450+" in names

    def test_dry_run_writes_nothing(self, session: Session):
        _seed_companies(session)
        _seed_chemistries(session)

        client = httpx.Client(transport=_build_mock_transport(), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                dry_run=True,
                client=client,
            )
        finally:
            client.close()

        assert stats["models_upserted"] == 0
        assert stats["chem_rows_upserted"] == 0
        # No models written.
        models = session.scalars(select(CompanyVehicleModel)).all()
        assert models == []

    def test_brands_filter_limits_processing(self, session: Session):
        _seed_companies(session)
        _seed_chemistries(session)

        client = httpx.Client(transport=_build_mock_transport(), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                brands=["Tesla"],
                client=client,
            )
        finally:
            client.close()

        # Only the Tesla variant is fetched and inserted.
        assert stats["models_upserted"] == 1
        models = session.scalars(select(CompanyVehicleModel)).all()
        assert len(models) == 1
        assert models[0].model_name.startswith("Tesla")

    def test_limit_caps_after_brand_filter(self, session: Session):
        _seed_companies(session)
        _seed_chemistries(session)

        client = httpx.Client(transport=_build_mock_transport(), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                limit=1,
                client=client,
            )
        finally:
            client.close()

        assert stats["discovered"] == 5
        # Only one variant is processed → at most one upsert OR skip.
        total_processed = (
            stats["models_upserted"]
            + stats["models_unchanged"]
            + stats["brand_skipped"]
            + stats["fetch_errors"]
            + stats["parse_errors"]
        )
        assert total_processed == 1


# ---------------------------------------------------------------------------
# 429 backoff + abort guard
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    def test_numeric_seconds(self):
        assert _parse_retry_after("12") == 12.0
        assert _parse_retry_after("0.5") == 0.5

    def test_negative_clamped_to_zero(self):
        assert _parse_retry_after("-3") == 0.0

    def test_none_or_garbage_returns_none(self):
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after("Wed, 01 Jan 2026 00:00:00 GMT") is None


class TestGetWithBackoff:
    def test_succeeds_first_try(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
        client = httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, text="ok")),
            base_url=BASE_URL,
        )
        try:
            r = _get_with_backoff(
                client,
                f"{BASE_URL}/car/1/x",
                max_retries=3,
                backoff_base=1.0,
                backoff_cap=10.0,
            )
        finally:
            client.close()
        assert r.status_code == 200
        assert slept == []

    def test_retries_then_succeeds(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
        attempts = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(429, text="slow down")
            return httpx.Response(200, text="ok")

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        try:
            r = _get_with_backoff(
                client,
                f"{BASE_URL}/car/1/x",
                max_retries=4,
                backoff_base=2.0,
                backoff_cap=100.0,
            )
        finally:
            client.close()
        assert r.status_code == 200
        # 2 retries → exponential 2.0, 4.0
        assert slept == [2.0, 4.0]

    def test_honors_retry_after_header(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
        attempts = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "7"}, text="x")
            return httpx.Response(200, text="ok")

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        try:
            r = _get_with_backoff(
                client,
                f"{BASE_URL}/car/1/x",
                max_retries=2,
                backoff_base=99.0,  # would dominate if Retry-After was ignored
                backoff_cap=999.0,
            )
        finally:
            client.close()
        assert r.status_code == 200
        assert slept == [7.0]

    def test_honors_backoff_cap(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))
        attempts = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(429, text="x")
            return httpx.Response(200, text="ok")

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        try:
            _get_with_backoff(
                client,
                f"{BASE_URL}/car/1/x",
                max_retries=4,
                backoff_base=100.0,
                backoff_cap=5.0,  # forces cap on every attempt
            )
        finally:
            client.close()
        assert slept == [5.0, 5.0]

    def test_raises_after_max_retries(self, monkeypatch):
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        client = httpx.Client(
            transport=httpx.MockTransport(lambda req: httpx.Response(429, text="x")),
            base_url=BASE_URL,
        )
        try:
            with pytest.raises(RateLimitedError):
                _get_with_backoff(
                    client,
                    f"{BASE_URL}/car/1/x",
                    max_retries=2,
                    backoff_base=1.0,
                    backoff_cap=10.0,
                )
        finally:
            client.close()

    def test_on_rate_limit_callback_invoked_per_429(self, monkeypatch):
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)
        attempts = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(429, text="x")
            return httpx.Response(200, text="ok")

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        hits = {"n": 0}
        try:
            _get_with_backoff(
                client,
                f"{BASE_URL}/car/1/x",
                max_retries=4,
                backoff_base=0.1,
                backoff_cap=0.1,
                on_rate_limit=lambda: hits.__setitem__("n", hits["n"] + 1),
            )
        finally:
            client.close()
        # Each 429 fires the callback (twice in this scenario).
        assert hits["n"] == 2


class TestRunRateLimitAbort:
    def test_aborts_after_consecutive_429_failures(self, session: Session, monkeypatch):
        # Discovery returns the cheatsheet; every detail URL returns 429 forever.
        cheatsheet = _read("cheatsheet_range.html")

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == DISCOVERY_PATH:
                return httpx.Response(200, text=cheatsheet)
            return httpx.Response(429, text="slow down")

        _seed_companies(session)
        _seed_chemistries(session)
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                client=client,
                max_retries=1,
                backoff_base=0.01,
                backoff_cap=0.01,
                abort_after_consecutive_429=2,
            )
        finally:
            client.close()

        assert stats["rate_limit_aborted"] is True
        # 2 URLs each hit 429 once before exhausting retries, then we abort.
        assert stats["fetch_errors"] == 2
        assert stats["models_upserted"] == 0
        assert stats["rate_limit_hits"] >= 2

    def test_disable_abort_runs_to_completion(self, session: Session, monkeypatch):
        # Same all-429 scenario, but with abort_after=0 we just keep going and
        # log every failure rather than bailing.
        cheatsheet = _read("cheatsheet_range.html")

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == DISCOVERY_PATH:
                return httpx.Response(200, text=cheatsheet)
            return httpx.Response(429, text="slow down")

        _seed_companies(session)
        _seed_chemistries(session)
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                client=client,
                max_retries=0,
                backoff_base=0.01,
                backoff_cap=0.01,
                abort_after_consecutive_429=0,  # disabled
            )
        finally:
            client.close()

        assert stats["rate_limit_aborted"] is False
        # All non-brand-skipped variants exhausted retries.
        assert stats["fetch_errors"] >= 1
        assert stats["models_upserted"] == 0


class TestRunRateLimitSleepGate:
    def test_sleep_gates_every_fetch_including_after_errors(
        self, session: Session, monkeypatch
    ):
        # Mix of OK + 404 + OK to ensure the rate-limit sleep fires before
        # every fetch from the 2nd onward, regardless of previous outcome.
        cheatsheet = _read("cheatsheet_range.html")
        bodies = {
            DISCOVERY_PATH: cheatsheet,
            "/car/2049/Tesla-Model-Y-RWD": _read("detail_lfp.html"),
            "/car/3110/BMW-iX-xDrive60": _read("detail_nmc.html"),
            "/car/2193/Mercedes-Benz-EQS-450plus": _read("detail_dual.html"),
            "/car/9999/Acme-Phantom-EV": _read("detail_no_chemistry.html"),
            "/car/3273/Lucid-Air-Grand-Touring": _read("detail_nmc.html"),
        }

        def handler(req: httpx.Request) -> httpx.Response:
            body = bodies.get(req.url.path)
            return httpx.Response(200 if body else 404, text=body or "x")

        _seed_companies(session)
        _seed_chemistries(session)
        slept: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: slept.append(s))

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        try:
            run(
                session,
                rate_limit_delay=0.5,
                client=client,
                max_retries=0,
                abort_after_consecutive_429=0,
            )
        finally:
            client.close()

        # 5 variants discovered → 3 actually fetched (2 brand_skipped: Acme +
        # Lucid). The first fetch skips the rate-limit sleep, the next two
        # apply it → exactly 2 sleeps of 0.5s.
        assert slept == [0.5, 0.5]


# ---------------------------------------------------------------------------
# Resumability: skip_existing + remaining_after_abort
# ---------------------------------------------------------------------------


def _seed_existing_variant(
    session: Session,
    *,
    company_id,
    car_id: int,
    model_name: str,
    chem_id: int,
    model_year_start: int = 2024,
    model_year_end: Optional[int] = None,
    chemistry_raw: str = "NMC",
) -> CompanyVehicleModel:
    """Insert a previously-scraped CompanyVehicleModel + chemistry row, the
    way ``upsert_variant`` would have left it.
    """
    model = CompanyVehicleModel(
        company_id=company_id,
        model_name=model_name,
        model_year_start=model_year_start,
        model_year_end=model_year_end,
        production_volume_units=None,
        is_active=True,
        data_source="ev-database.org",
        metadata_json={
            "ev_database_id": car_id,
            "source_url": f"{BASE_URL}/car/{car_id}/{model_name.replace(' ', '-')}",
            "useable_battery_kwh": 108.9,
            "chemistry_raw": chemistry_raw,
            "is_discontinued": False,
        },
    )
    session.add(model)
    session.flush()
    session.add(
        VehicleModelChemistry(
            vehicle_model_id=model.id,
            battery_chemistry_id=chem_id,
            share_pct=1.0,
            valid_from=date(model_year_start, 1, 1),
            valid_to=None,
        )
    )
    session.flush()
    return model


class TestAlreadyStoredCarIds:
    def test_empty_when_no_rows(self, session: Session):
        assert _already_stored_car_ids(session) == set()

    def test_collects_ids_only_from_ev_database_rows(self, session: Session):
        companies = _seed_companies(session)
        chems = _seed_chemistries(session)

        # ev-database row (should be picked up).
        _seed_existing_variant(
            session,
            company_id=companies["BMW Group"].id,
            car_id=3110,
            model_name="BMW iX xDrive60",
            chem_id=chems["nmc"].id,
        )

        # Same shape but different data_source → MUST NOT leak in.
        other = CompanyVehicleModel(
            company_id=companies["Tesla"].id,
            model_name="Tesla Model S",
            model_year_start=2024,
            data_source="manual_seed",
            metadata_json={"ev_database_id": 999},
        )
        session.add(other)
        session.flush()

        # ev-database row but with no metadata at all → ignored gracefully.
        bare = CompanyVehicleModel(
            company_id=companies["Volkswagen Group"].id,
            model_name="VW ID.7",
            model_year_start=2024,
            data_source="ev-database.org",
            metadata_json=None,
        )
        session.add(bare)
        session.flush()

        # ev-database row whose ev_database_id is a string → ignored.
        weird = CompanyVehicleModel(
            company_id=companies["Mercedes-Benz Group"].id,
            model_name="Merc EQS",
            model_year_start=2024,
            data_source="ev-database.org",
            metadata_json={"ev_database_id": "not-an-int"},
        )
        session.add(weird)
        session.flush()

        assert _already_stored_car_ids(session) == {3110}


class TestRunSkipExisting:
    def test_pre_seeded_variant_is_pruned_before_any_fetch(
        self, session: Session
    ):
        # Pre-seed the BMW variant; the run should NOT fetch its detail page.
        companies = _seed_companies(session)
        chems = _seed_chemistries(session)
        _seed_existing_variant(
            session,
            company_id=companies["BMW Group"].id,
            car_id=3110,
            model_name="BMW iX xDrive60",
            chem_id=chems["nmc"].id,
        )

        # Tracking transport: record every URL hit so we can assert BMW was skipped.
        cheatsheet = _read("cheatsheet_range.html")
        bodies = {
            DISCOVERY_PATH: cheatsheet,
            "/car/2049/Tesla-Model-Y-RWD": _read("detail_lfp.html"),
            "/car/3110/BMW-iX-xDrive60": _read("detail_nmc.html"),
            "/car/2193/Mercedes-Benz-EQS-450plus": _read("detail_dual.html"),
            "/car/9999/Acme-Phantom-EV": _read("detail_no_chemistry.html"),
            "/car/3273/Lucid-Air-Grand-Touring": _read("detail_nmc.html"),
        }
        hit_paths: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            hit_paths.append(req.url.path)
            body = bodies.get(req.url.path)
            return httpx.Response(200 if body else 404, text=body or "x")

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                client=client,
                max_retries=0,
                abort_after_consecutive_429=0,
            )
        finally:
            client.close()

        # Discovery hit + 2 detail hits (Tesla, Mercedes). BMW was pre-seeded
        # → skipped. Acme + Lucid get brand-skipped before fetch.
        assert "/car/3110/BMW-iX-xDrive60" not in hit_paths
        assert stats["already_stored_skipped"] == 1
        assert stats["models_upserted"] == 2

    def test_no_skip_existing_re_fetches_everything(self, session: Session):
        companies = _seed_companies(session)
        chems = _seed_chemistries(session)
        # Year + chemistry must match what detail_nmc.html parses to so the
        # second upsert sees an unchanged row rather than inserting a new one.
        _seed_existing_variant(
            session,
            company_id=companies["BMW Group"].id,
            car_id=3110,
            model_name="BMW iX xDrive60",
            chem_id=chems["nmc"].id,
            model_year_start=2025,
            model_year_end=2026,
        )

        client = httpx.Client(transport=_build_mock_transport(), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                client=client,
                skip_existing=False,  # ← key flag
                max_retries=0,
                abort_after_consecutive_429=0,
            )
        finally:
            client.close()

        assert stats["already_stored_skipped"] == 0
        # BMW row was already there → upsert_variant treats it as unchanged.
        # Tesla + Mercedes are new → upserted. So unchanged=1, upserted=2.
        assert stats["models_unchanged"] == 1
        assert stats["models_upserted"] == 2

    def test_dry_run_with_skip_existing_does_not_pollute_state(
        self, session: Session
    ):
        # Sanity: dry-run + skip-existing still only attempts the missing ones.
        companies = _seed_companies(session)
        chems = _seed_chemistries(session)
        _seed_existing_variant(
            session,
            company_id=companies["BMW Group"].id,
            car_id=3110,
            model_name="BMW iX xDrive60",
            chem_id=chems["nmc"].id,
        )

        cheatsheet = _read("cheatsheet_range.html")
        bodies = {
            DISCOVERY_PATH: cheatsheet,
            "/car/2049/Tesla-Model-Y-RWD": _read("detail_lfp.html"),
            "/car/3110/BMW-iX-xDrive60": _read("detail_nmc.html"),
            "/car/2193/Mercedes-Benz-EQS-450plus": _read("detail_dual.html"),
            "/car/9999/Acme-Phantom-EV": _read("detail_no_chemistry.html"),
            "/car/3273/Lucid-Air-Grand-Touring": _read("detail_nmc.html"),
        }
        hit_paths: list[str] = []

        def handler(req: httpx.Request) -> httpx.Response:
            hit_paths.append(req.url.path)
            body = bodies.get(req.url.path)
            return httpx.Response(200 if body else 404, text=body or "x")

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                client=client,
                dry_run=True,
                max_retries=0,
                abort_after_consecutive_429=0,
            )
        finally:
            client.close()

        assert stats["already_stored_skipped"] == 1
        assert "/car/3110/BMW-iX-xDrive60" not in hit_paths


class TestRunRemainingAfterAbort:
    def test_remaining_after_abort_reflects_unattempted_variants(
        self, session: Session, monkeypatch
    ):
        # 5 variants discovered → 2 brand-skipped before any fetch. The next 2
        # consecutive 429s trigger the abort, leaving 1 variant untried.
        cheatsheet = _read("cheatsheet_range.html")

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path == DISCOVERY_PATH:
                return httpx.Response(200, text=cheatsheet)
            return httpx.Response(429, text="slow down")

        _seed_companies(session)
        _seed_chemistries(session)
        monkeypatch.setattr(mod.time, "sleep", lambda s: None)

        client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
        try:
            stats = run(
                session,
                rate_limit_delay=0,
                client=client,
                max_retries=0,
                backoff_base=0.01,
                backoff_cap=0.01,
                abort_after_consecutive_429=2,
            )
        finally:
            client.close()

        assert stats["rate_limit_aborted"] is True
        # Discovery: 5. Brand-skipped: Acme + Lucid (Lucid Group not seeded) = 2.
        # 2 fetch attempts both fail → abort. 1 variant left untried.
        assert stats["fetch_errors"] == 2
        assert stats["remaining_after_abort"] == 1
