"""Tests for the UN Comtrade ingestion module.

All tests are self-contained — no real API calls and no live database.
``parse_comtrade_rows`` is tested with static raw-row fixtures.
``_build_hs_material_map`` and ``ingest_comtrade`` are tested with mocked sessions.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest

from app.services.ingestion.comtrade import (
    _build_hs_material_map,
    _external_id,
    _resolve_material_id,
    parse_comtrade_rows,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _raw_row(
    cmd_code: str = "850760",
    cmd_desc: str = "Lithium-ion batteries",
    primary_value: float = 1_500_000.0,
    net_wgt: float = 25_000.0,
    partner_code: int = 0,
) -> dict:
    """Return a minimal Comtrade API data row dict."""
    return {
        "cmdCode": cmd_code,
        "cmdDesc": cmd_desc,
        "primaryValue": primary_value,
        "netWgt": net_wgt,
        "partnerCode": partner_code,
    }


# ---------------------------------------------------------------------------
# parse_comtrade_rows
# ---------------------------------------------------------------------------

class TestParseComtradeRows:
    def test_period_set_from_year(self):
        results = parse_comtrade_rows([_raw_row()], "CN", "8507", 2023)
        assert results[0]["period"] == "2023"

    def test_reporter_country_is_iso2(self):
        results = parse_comtrade_rows([_raw_row()], "CN", "8507", 2023)
        assert results[0]["reporter_country"] == "CN"

    def test_import_export_flag_is_export(self):
        results = parse_comtrade_rows([_raw_row()], "CN", "8507", 2023)
        assert results[0]["import_export_flag"] == "export"

    def test_hs_code_from_cmd_code_not_prefix(self):
        """hs_code in the result must be the 6-digit cmdCode, not the queried prefix."""
        results = parse_comtrade_rows([_raw_row(cmd_code="850760")], "CN", "8507", 2023)
        assert results[0]["hs_code"] == "850760"

    def test_hs_description_from_cmd_desc(self):
        results = parse_comtrade_rows([_raw_row(cmd_desc="Test batteries")], "CN", "8507", 2023)
        assert results[0]["hs_description"] == "Test batteries"

    def test_trade_value_usd_from_primary_value(self):
        results = parse_comtrade_rows([_raw_row(primary_value=999_000.0)], "CN", "8507", 2023)
        assert results[0]["trade_value_usd"] == pytest.approx(999_000.0)

    def test_quantity_from_net_wgt(self):
        results = parse_comtrade_rows([_raw_row(net_wgt=5_000.0)], "CN", "8507", 2023)
        assert results[0]["quantity"] == pytest.approx(5_000.0)
        assert results[0]["quantity_unit"] == "kg"

    def test_partner_code_zero_maps_to_wld(self):
        results = parse_comtrade_rows([_raw_row(partner_code=0)], "CN", "8507", 2023)
        assert results[0]["partner_country"] == "WLD"

    def test_known_partner_code_maps_to_iso2(self):
        """partnerCode 842 = US."""
        results = parse_comtrade_rows([_raw_row(partner_code=842)], "CN", "8507", 2023)
        assert results[0]["partner_country"] == "US"

    def test_unknown_partner_code_kept_as_string(self):
        results = parse_comtrade_rows([_raw_row(partner_code=9999)], "CN", "8507", 2023)
        assert results[0]["partner_country"] == "9999"

    def test_metadata_json_contains_expected_keys(self):
        results = parse_comtrade_rows([_raw_row()], "AU", "2604", 2022)
        meta = results[0]["metadata_json"]
        assert meta["comtrade_flow_code"] == "X"
        assert meta["comtrade_period"] == 2022
        assert meta["hs_prefix_queried"] == "2604"

    def test_skips_row_with_zero_value_and_no_quantity(self):
        row = _raw_row(primary_value=0.0, net_wgt=0.0)
        results = parse_comtrade_rows([row], "CN", "8507", 2023)
        assert results == []

    def test_skips_row_with_none_value_and_none_quantity(self):
        row = {
            "cmdCode": "850760",
            "cmdDesc": "Batteries",
            "primaryValue": None,
            "netWgt": None,
            "partnerCode": 0,
        }
        results = parse_comtrade_rows([row], "CN", "8507", 2023)
        assert results == []

    def test_keeps_row_with_value_but_no_quantity(self):
        row = _raw_row(primary_value=500_000.0, net_wgt=None)
        row["netWgt"] = None
        results = parse_comtrade_rows([row], "CN", "8507", 2023)
        assert len(results) == 1
        assert results[0]["quantity"] is None
        assert results[0]["quantity_unit"] is None

    def test_keeps_row_with_quantity_but_no_value(self):
        row = {
            "cmdCode": "260400",
            "cmdDesc": "Nickel ore",
            "primaryValue": None,
            "netWgt": 1_000_000.0,
            "partnerCode": 0,
        }
        results = parse_comtrade_rows([row], "ID", "2604", 2023)
        assert len(results) == 1
        assert results[0]["trade_value_usd"] is None

    def test_multiple_rows_all_returned(self):
        rows = [_raw_row(cmd_code=f"85070{i}") for i in range(3)]
        results = parse_comtrade_rows(rows, "CN", "8507", 2023)
        assert len(results) == 3

    def test_empty_rows_returns_empty_list(self):
        assert parse_comtrade_rows([], "CN", "8507", 2023) == []


# ---------------------------------------------------------------------------
# _build_hs_material_map and _resolve_material_id
# ---------------------------------------------------------------------------

def _mock_hs_row(
    prefix: str,
    material_id: int,
    *,
    confidence: float = 1.0,
    mapping_id: int | None = None,
) -> MagicMock:
    """Build a mock ``HsCodeMaterialMapping`` row.

    Carries ``hs_code_prefix``, ``material_id``, ``confidence`` and ``id``
    — the four fields ``_build_hs_material_map`` reads off each row.
    ``mapping_id`` defaults to a stable hash-derived value when omitted so
    tests don't have to set it explicitly when they don't care.
    """
    row = MagicMock()
    row.hs_code_prefix = prefix
    row.material_id = material_id
    row.confidence = confidence
    # Stable but non-colliding default — distinct per (prefix, material_id).
    row.id = (
        mapping_id
        if mapping_id is not None
        else hash((prefix, material_id)) & 0xFFFF
    )
    return row


def _mock_session_for_hs_map(hs_rows: list) -> MagicMock:
    session = MagicMock()
    scalars_result = MagicMock()
    scalars_result.all.return_value = hs_rows
    session.scalars.return_value = scalars_result
    return session


class TestBuildHsMaterialMap:
    """``_build_hs_material_map`` returns ``dict[str, list[tuple[int, float, int]]]``.

    Each entry is a list of ``(material_id, confidence, hs_mapping_id)``
    tuples — a single prefix can resolve to multiple materials when the
    underlying HS code covers more than one (e.g. ores-and-concentrates
    catch-alls).  Confidence and hs_mapping_id flow through to
    ``_resolve_material_id`` for downstream relevance weighting.
    """

    def test_returns_prefix_to_tuple_list_dict(self):
        rows = [
            _mock_hs_row("8507", 1, confidence=1.0, mapping_id=10),
            _mock_hs_row("2604", 2, confidence=0.8, mapping_id=20),
        ]
        session = _mock_session_for_hs_map(rows)
        result = _build_hs_material_map(session)

        assert result["8507"] == [(1, 1.0, 10)]
        assert result["2604"] == [(2, 0.8, 20)]

    def test_multiple_materials_per_prefix_collated(self):
        """Shared 4-digit prefix → both candidates appear in the list."""
        rows = [
            _mock_hs_row("2615", 3, confidence=0.5, mapping_id=30),
            _mock_hs_row("2615", 4, confidence=0.5, mapping_id=31),
        ]
        session = _mock_session_for_hs_map(rows)
        result = _build_hs_material_map(session)

        assert sorted(result["2615"]) == [(3, 0.5, 30), (4, 0.5, 31)]

    def test_dots_stripped_from_prefix(self):
        """Stored ``"85.07"`` is keyed without dots so it can be compared
        against raw HS codes that come from Comtrade without dots."""
        row = _mock_hs_row("85.07", 1, confidence=1.0, mapping_id=10)
        session = _mock_session_for_hs_map([row])
        result = _build_hs_material_map(session)

        assert "8507" in result
        assert "85.07" not in result

    def test_empty_mappings_returns_empty_dict(self):
        session = _mock_session_for_hs_map([])
        assert _build_hs_material_map(session) == {}


class TestResolveMaterialId:
    """``_resolve_material_id`` returns ``(material_id, hs_mapping_id, confidence)``.

    The third element (confidence) was added 2026-05-09 as part of the
    Tier 1.4 audit extension so callers can downscale
    ``RiskEventMaterial.relevance_score`` for low-confidence prefix
    matches.  Callers that don't need confidence can ignore it:
        ``mid, hs_id, _ = _resolve_material_id(code, hs_map)``.
    """

    def test_exact_six_digit_match(self):
        hs_map = {"850760": [(1, 1.0, 10)]}
        assert _resolve_material_id("850760", hs_map) == (1, 10, 1.0)

    def test_four_digit_prefix_fallback(self):
        """``850760`` has no exact match; falls back to the 4-digit ``8507``."""
        hs_map = {"8507": [(1, 1.0, 10)]}
        assert _resolve_material_id("850760", hs_map) == (1, 10, 1.0)

    def test_confidence_flows_through_low_confidence_mapping(self):
        """Low-confidence prefix matches return their actual confidence so
        downstream callers can downweight relevance accordingly."""
        hs_map = {"2530": [(1, 0.4, 11)]}
        result = _resolve_material_id("253099", hs_map)
        assert result == (1, 11, 0.4)

    def test_unrecognised_code_returns_all_none(self):
        hs_map = {"8507": [(1, 1.0, 10)]}
        assert _resolve_material_id("280450", hs_map) == (None, None, None)

    def test_empty_code_returns_all_none(self):
        hs_map = {"8507": [(1, 1.0, 10)]}
        assert _resolve_material_id("", hs_map) == (None, None, None)

    def test_longer_prefix_not_confused_with_shorter(self):
        hs_map = {"2836": [(3, 1.0, 30)]}
        assert _resolve_material_id("283610", hs_map) == (3, 30, 1.0)
        assert _resolve_material_id("280450", hs_map) == (None, None, None)

    def test_multiple_candidates_highest_confidence_wins(self):
        """When a prefix has multiple candidates and one has a higher
        confidence than the others, that one wins."""
        hs_map = {
            "2615": [
                (3, 0.4, 30),
                (4, 0.9, 31),  # highest confidence
                (5, 0.4, 32),
            ]
        }
        assert _resolve_material_id("2615", hs_map) == (4, 31, 0.9)

    def test_tied_top_confidence_returns_all_none(self):
        """Ambiguity at the same confidence level is honest NULL rather
        than an arbitrary pick that silently poisons scoring."""
        hs_map = {
            "2615": [
                (3, 0.5, 30),
                (4, 0.5, 31),  # tied with the first row
            ]
        }
        assert _resolve_material_id("2615", hs_map) == (None, None, None)


# ---------------------------------------------------------------------------
# _external_id
# ---------------------------------------------------------------------------

class TestExternalId:
    def test_format(self):
        # v2 format (post-2026-06): includes version tag + breakdown_mode.
        # flow_code defaults to "X" (exports); breakdown_mode defaults to
        # the module-level _BREAKDOWN_MODE ("plus").  The flow_code and
        # breakdown_mode are part of the key so different combinations
        # for the same reporter/HS/year don't trip each other's
        # idempotency check.
        assert _external_id("CN", "8507", 2023) == "comtrade_v2_X_A_HS_plus_8507_CN_2023"

    def test_flow_code_distinguishes_export_from_import(self):
        export_id = _external_id("CN", "8507", 2023, flow_code="X")
        import_id = _external_id("CN", "8507", 2023, flow_code="M")
        assert export_id != import_id

    def test_breakdown_mode_distinguishes_plus_from_classic(self):
        plus_id = _external_id("CN", "8507", 2023, breakdown_mode="plus")
        classic_id = _external_id("CN", "8507", 2023, breakdown_mode="classic")
        assert plus_id != classic_id

    def test_unique_per_combination(self):
        ids = {
            _external_id("CN", "8507", 2021),
            _external_id("CN", "8507", 2022),
            _external_id("CN", "2604", 2021),
            _external_id("AU", "8507", 2021),
        }
        assert len(ids) == 4


# ---------------------------------------------------------------------------
# ingest_comtrade — integration-style with mocked API and session
# ---------------------------------------------------------------------------

def _make_ingest_session(
    existing_doc: bool = False,
    source_id: int = 1,
    has_hs_mappings: bool = True,
) -> MagicMock:
    """Build a mock session for ingest_comtrade tests.

    All tests pass ``hs_prefixes`` explicitly so the SupplyChainContext call
    never fires. Call order for one reporter × one prefix × one year:

    1. Source lookup  (_get_or_create_comtrade_source)
    2. SourceDocument idempotency check  (outer loop)
    3. SourceDocument lookup inside _create_source_document  (when not skipping)
    """
    mock_source = MagicMock()
    mock_source.id = source_id

    mock_doc = MagicMock() if existing_doc else None

    hs_rows = [_mock_hs_row("8507", 1)] if has_hs_mappings else []

    call_tracker = {"n": 0}

    def scalar_side_effect(stmt):
        call_tracker["n"] += 1
        n = call_tracker["n"]
        if n == 1:
            return mock_source   # Source lookup
        if n == 2:
            return mock_doc      # SourceDocument idempotency check
        return None              # _create_source_document or subsequent iterations

    scalars_result = MagicMock()
    scalars_result.all.return_value = hs_rows

    session = MagicMock()
    session.scalar.side_effect = scalar_side_effect
    session.scalars.return_value = scalars_result

    def add_side_effect(obj):
        if hasattr(obj, "id") and obj.id is None:
            obj.id = 999

    session.add.side_effect = add_side_effect
    return session


class TestIngestComtrade:
    def _three_raw_rows(self) -> list[dict]:
        return [_raw_row(cmd_code=f"85070{i}") for i in range(3)]

    def test_raises_if_no_api_key(self):
        from app.services.ingestion.comtrade import ingest_comtrade

        session = MagicMock()
        with patch("app.services.ingestion.comtrade.get_settings") as mock_settings:
            mock_settings.return_value.comtrade_api_key = ""
            mock_settings.return_value.comtrade_rate_limit_delay = 0
            mock_settings.return_value.comtrade_base_url = "https://example.com"
            with pytest.raises(ValueError, match="COMTRADE_API_KEY"):
                ingest_comtrade(
                    session=session,
                    years=[2023],
                    reporters={"CN": 156},
                    hs_prefixes=["8507"],
                )

    def test_inserted_count_matches_parsed_rows(self):
        from app.services.ingestion.comtrade import ingest_comtrade

        session = _make_ingest_session(existing_doc=False)

        with (
            patch("app.services.ingestion.comtrade.get_settings") as mock_settings,
            patch("app.services.ingestion.comtrade.fetch_annual_exports", return_value=self._three_raw_rows()),
            patch("app.services.ingestion.comtrade.time.sleep"),
        ):
            mock_settings.return_value.comtrade_api_key = "test-key"
            mock_settings.return_value.comtrade_rate_limit_delay = 0
            mock_settings.return_value.comtrade_base_url = "https://example.com"
            result = ingest_comtrade(
                session=session,
                years=[2023],
                reporters={"CN": 156},
                hs_prefixes=["8507"],
            )

        assert result["inserted"] == 3
        assert result["skipped_existing_doc"] == 0
        assert result["api_calls_made"] == 1

    def test_skips_existing_doc(self):
        """When a SourceDocument already exists, the batch is skipped entirely."""
        from app.services.ingestion.comtrade import ingest_comtrade

        session = _make_ingest_session(existing_doc=True)

        with (
            patch("app.services.ingestion.comtrade.get_settings") as mock_settings,
            patch("app.services.ingestion.comtrade.fetch_annual_exports") as mock_fetch,
            patch("app.services.ingestion.comtrade.time.sleep"),
        ):
            mock_settings.return_value.comtrade_api_key = "test-key"
            mock_settings.return_value.comtrade_rate_limit_delay = 0
            mock_settings.return_value.comtrade_base_url = "https://example.com"
            result = ingest_comtrade(
                session=session,
                years=[2023],
                reporters={"CN": 156},
                hs_prefixes=["8507"],
            )

        assert result["skipped_existing_doc"] == 1
        assert result["inserted"] == 0
        mock_fetch.assert_not_called()

    def test_skipped_empty_response(self):
        from app.services.ingestion.comtrade import ingest_comtrade

        session = _make_ingest_session(existing_doc=False)

        with (
            patch("app.services.ingestion.comtrade.get_settings") as mock_settings,
            patch("app.services.ingestion.comtrade.fetch_annual_exports", return_value=[]),
            patch("app.services.ingestion.comtrade.time.sleep"),
        ):
            mock_settings.return_value.comtrade_api_key = "test-key"
            mock_settings.return_value.comtrade_rate_limit_delay = 0
            mock_settings.return_value.comtrade_base_url = "https://example.com"
            result = ingest_comtrade(
                session=session,
                years=[2023],
                reporters={"CN": 156},
                hs_prefixes=["8507"],
            )

        assert result["skipped_empty_response"] == 1
        assert result["inserted"] == 0

    def test_api_error_increments_errors_does_not_abort(self):
        """An API error for one combination should not abort the rest of the run."""
        from app.services.ingestion.comtrade import ingest_comtrade
        import httpx

        # Two reporters; first raises, second succeeds
        call_count = {"n": 0}
        def fake_fetch(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise httpx.HTTPStatusError("429", request=MagicMock(), response=MagicMock())
            return _three_raw_rows()

        def _three_raw_rows():
            return [_raw_row(cmd_code=f"85070{i}") for i in range(2)]

        # Need a session that handles 2 reporters (more scalar calls)
        session = MagicMock()
        mock_source = MagicMock()
        mock_source.id = 1
        mock_doc_new = MagicMock()
        mock_doc_new.id = 999

        call_tracker = {"n": 0}
        def scalar_se(stmt):
            call_tracker["n"] += 1
            n = call_tracker["n"]
            if n == 1:
                return mock_source   # Source lookup
            if n in (2, 5):
                return None          # SourceDocument idempotency check (not existing)
            if n in (3, 6):
                return None          # inside _create_source_document
            return None

        scalars_result = MagicMock()
        scalars_result.all.return_value = []
        session.scalar.side_effect = scalar_se
        session.scalars.return_value = scalars_result

        with (
            patch("app.services.ingestion.comtrade.get_settings") as mock_settings,
            patch("app.services.ingestion.comtrade.fetch_annual_exports", side_effect=fake_fetch),
            patch("app.services.ingestion.comtrade.time.sleep"),
        ):
            mock_settings.return_value.comtrade_api_key = "test-key"
            mock_settings.return_value.comtrade_rate_limit_delay = 0
            mock_settings.return_value.comtrade_base_url = "https://example.com"
            result = ingest_comtrade(
                session=session,
                years=[2023],
                reporters={"CN": 156, "AU": 36},
                hs_prefixes=["8507"],
            )

        assert result["errors"] == 1
        assert result["inserted"] == 2  # second reporter succeeded

    def test_rate_limit_sleep_called_before_each_api_call(self):
        from app.services.ingestion.comtrade import ingest_comtrade

        session = _make_ingest_session(existing_doc=False)

        with (
            patch("app.services.ingestion.comtrade.get_settings") as mock_settings,
            patch("app.services.ingestion.comtrade.fetch_annual_exports", return_value=[_raw_row()]),
            patch("app.services.ingestion.comtrade.time.sleep") as mock_sleep,
        ):
            mock_settings.return_value.comtrade_api_key = "test-key"
            mock_settings.return_value.comtrade_rate_limit_delay = 1.5
            mock_settings.return_value.comtrade_base_url = "https://example.com"
            ingest_comtrade(
                session=session,
                years=[2023],
                reporters={"CN": 156},
                hs_prefixes=["8507"],
            )

        mock_sleep.assert_called_once_with(1.5)

    def test_session_commit_called_when_rows_inserted(self):
        """Commit is called once per batch that has rows to insert."""
        from app.services.ingestion.comtrade import ingest_comtrade

        session = _make_ingest_session(existing_doc=False)

        with (
            patch("app.services.ingestion.comtrade.get_settings") as mock_settings,
            patch("app.services.ingestion.comtrade.fetch_annual_exports", return_value=[_raw_row()]),
            patch("app.services.ingestion.comtrade.time.sleep"),
        ):
            mock_settings.return_value.comtrade_api_key = "test-key"
            mock_settings.return_value.comtrade_rate_limit_delay = 0
            mock_settings.return_value.comtrade_base_url = "https://example.com"
            ingest_comtrade(
                session=session,
                years=[2023],
                reporters={"CN": 156},
                hs_prefixes=["8507"],
            )

        # One batch with one row → exactly one commit
        session.commit.assert_called_once()

    def test_empty_response_commits_marker_source_document(self):
        """6.2 fix (2026-06): an empty API response now commits an empty
        SourceDocument so the next run skips the same (reporter, prefix, year)
        combination via the idempotency check rather than wasting another
        daily-quota call on a chronically-empty combination.
        """
        from app.services.ingestion.comtrade import ingest_comtrade

        session = _make_ingest_session(existing_doc=False)

        with (
            patch("app.services.ingestion.comtrade.get_settings") as mock_settings,
            patch("app.services.ingestion.comtrade.fetch_annual_exports", return_value=[]),
            patch("app.services.ingestion.comtrade.time.sleep"),
        ):
            mock_settings.return_value.comtrade_api_key = "test-key"
            mock_settings.return_value.comtrade_rate_limit_delay = 0
            mock_settings.return_value.comtrade_base_url = "https://example.com"
            result = ingest_comtrade(
                session=session,
                years=[2023],
                reporters={"CN": 156},
                hs_prefixes=["8507"],
            )

        # One empty-marker commit per (reporter × prefix × year × flow).
        session.commit.assert_called_once()
        assert result["inserted"] == 0
        assert result["skipped_empty_response"] == 1
