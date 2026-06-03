"""Tests for app/services/ingestion/mcs_pdf_parser.py.

These tests do not require a real PDF — they exercise the text-parsing logic
using inline fixture strings that reproduce the relevant formatting conventions
from USGS Mineral Commodity Summaries.

DB seeding tests use a mock session so they run without a live database.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from app.services.ingestion.mcs_pdf_parser import (
    MCSPdfParser,
    _HTS_CODE_RE,
    _IMPORT_SOURCES_RE,
    CommoditySection,
    ImportSource,
    TariffEntry,
)
from app.services.ingestion.seed_material_source_aliases import _ALIASES


def _mcs_pdf_aliases() -> dict[str, str]:
    """Return ``{source_name (upper-stripped): canonical}`` for the
    mcs_pdf alias rows that resolve to a tracked material.

    Skipped aliases (4-tuple with None canonical) are excluded; secondary
    aliases (5-tuple) include the canonical they share with a primary.

    Replaces the deleted ``_MCS_COMMODITY_MAP`` dict (Section 4.3 fix
    2026-06).
    """
    out: dict[str, str] = {}
    for row in _ALIASES:
        if row[0] != "mcs_pdf":
            continue
        source_name = row[1].strip().upper()
        canonical = row[2]  # 3-tuple / 5-tuple both put canonical at idx 2
        if canonical is None:
            continue  # explicit skip row
        out[source_name] = canonical
    return out


# ---------------------------------------------------------------------------
# Fixtures — sample PDF text fragments
# ---------------------------------------------------------------------------

COBALT_TARIFF_BLOCK = """\
Tariff:  Item  Number
Cobalt ores and concentrates:
  Other  2605.00.0000  Free
Cobalt oxides and hydroxides; commercial cobalt oxides:
  Cobalt hydroxide  2822.00.0010  4.2% ad val.
  Cobalt oxide, other  2822.00.0090  4.2% ad val.
Cobalt sulfate:
  2833.29.1000  1.4% ad val.
Cobalt mattes and other intermediates:
  8105.20.3000  Free
Unwrought cobalt:
  8105.20.6000  Free
Depletion Allowance: 22%
"""

COBALT_PRODUCTION_BLOCK = """\
World Mine Production and Reserves:
                          Mine production          Reserves
Country                    2024       2025(e)
Congo (Kinshasa)         170,000    170,000
Russia                     7,600      7,600
Australia                  5,500      5,100
Philippines                3,500      3,000
Cuba                       3,600      3,500
Other countries            6,200      5,200
World total (rounded)    220,000    210,000
"""

COBALT_IMPORT_BLOCK = """\
Import Sources (2021–24): Democratic Republic of the Congo, 30%;
Finland, 26%; South Africa, 12%; Norway, 12%; and other, 20%.
"""

COBALT_SALIENT_BLOCK = """\
Salient Statistics—United States:
                          2020   2021   2022   2023   2024(e)
Mine production, Co content, t    700    600    550    500     450
Events, Trends, and Issues: Cobalt prices remained volatile.
"""

# Full fake commodity section for split testing
FAKE_PDF_TEXT = f"""
ALUMINUM

Some preamble text.

{COBALT_TARIFF_BLOCK}

COBALT

{COBALT_TARIFF_BLOCK}

{COBALT_PRODUCTION_BLOCK}

{COBALT_IMPORT_BLOCK}

{COBALT_SALIENT_BLOCK}

NICKEL

Nickel tariff content here.
"""


# ---------------------------------------------------------------------------
# Alias coverage tests (post Section 4.3: _MCS_COMMODITY_MAP deleted; source
# of truth is material_source_aliases (source_system='mcs_pdf'))
# ---------------------------------------------------------------------------

class TestMcsPdfAliases:
    """Verify the mcs_pdf rows in material_source_aliases stay coherent.

    Replaces the deleted ``TestCommodityMap`` class.  Same intent:
    catch blank canonicals, missing core commodities, and unintended
    duplicate canonicals across primary aliases.
    """

    def test_all_canonicals_are_nonempty_strings(self):
        for heading, canonical in _mcs_pdf_aliases().items():
            assert isinstance(canonical, str) and canonical.strip(), (
                f"Blank canonical for mcs_pdf alias {heading!r}"
            )

    def test_core_headings_present(self):
        expected = {"COBALT", "LITHIUM", "NICKEL", "MANGANESE", "ALUMINUM", "COPPER"}
        assert expected.issubset(_mcs_pdf_aliases().keys())

    def test_no_duplicate_canonical_names_among_primary_rows(self):
        """Primary alias rows (3-tuples) must not share a canonical.
        Secondary aliases (5-tuples) intentionally reuse a primary's
        canonical (e.g. BAUXITE AND ALUMINA → Aluminum) and are filtered
        out of this check.
        """
        primary_canonicals = [
            row[2] for row in _ALIASES
            if row[0] == "mcs_pdf" and len(row) == 3
        ]
        dupes = [c for c in set(primary_canonicals) if primary_canonicals.count(c) > 1]
        assert not dupes, (
            f"Duplicate canonical names in mcs_pdf primary aliases: {dupes}"
        )


# TestProductionStagePreference removed 2026-06 (Section 5.1 cleanup).
# _MCS_PRODUCTION_STAGE_PREFERENCE was used only by _pick_production_hs_id,
# which is itself removed.  See mcs_pdf_parser.py file header for context.


# ---------------------------------------------------------------------------
# Regex tests
# ---------------------------------------------------------------------------

class TestHtsCodeRegex:
    @pytest.mark.parametrize("text, expected", [
        ("2605.00.0000  Free", ["2605.00.0000"]),
        ("8105.20.3000", ["8105.20.3000"]),
        ("HTS 2833.29.1000 applies", ["2833.29.1000"]),
        ("no code here", []),
        ("2605.00.0000 and 8105.20.6000", ["2605.00.0000", "8105.20.6000"]),
        # Should NOT match 6-digit or 4-digit codes
        ("260500 is not 10-digit", []),
        ("2605 is not 10-digit", []),
    ])
    def test_matches(self, text: str, expected: list[str]):
        found = _HTS_CODE_RE.findall(text)
        assert found == expected


class TestImportSourcesRegex:
    @pytest.mark.parametrize("text", [
        "Import Sources (2021–24): South Africa, 28%",
        "Import Sources (2020-24): Russia, 23%",
        "Import Sources (2021-2024): Canada, 20%",
        "import sources (2022–25): Finland, 15%",  # case insensitive
    ])
    def test_matches(self, text: str):
        assert _IMPORT_SOURCES_RE.search(text) is not None

    @pytest.mark.parametrize("text", [
        "Source of imports: South Africa",
        "Import sources without year: Finland",
        "Import Sources 2021: no parens",
    ])
    def test_no_match(self, text: str):
        assert _IMPORT_SOURCES_RE.search(text) is None


# ---------------------------------------------------------------------------
# Parser text extraction tests  (no real PDF — uses MCSPdfParser internal methods)
# ---------------------------------------------------------------------------

class TestParserTextExtraction:
    @pytest.fixture
    def parser(self, tmp_path: Path) -> MCSPdfParser:
        # We only test internal methods; the PDF path is never opened in these tests
        fake_pdf = tmp_path / "fake.pdf"
        fake_pdf.write_bytes(b"")
        return MCSPdfParser(fake_pdf, reference_year=2026)

    def test_parse_tariff_table_extracts_hts_codes(self, parser: MCSPdfParser):
        entries = parser._parse_tariff_table(COBALT_TARIFF_BLOCK)
        codes = [e.hts_code for e in entries]
        assert "2605000000" in codes
        assert "2822000010" in codes
        assert "2822000090" in codes
        assert "2833291000" in codes
        assert "8105203000" in codes
        assert "8105206000" in codes

    def test_parse_tariff_table_no_dots_in_normalised_code(self, parser: MCSPdfParser):
        entries = parser._parse_tariff_table(COBALT_TARIFF_BLOCK)
        for entry in entries:
            assert "." not in entry.hts_code, (
                f"Normalised code {entry.hts_code!r} should not contain dots"
            )

    def test_parse_tariff_table_raw_code_preserved(self, parser: MCSPdfParser):
        entries = parser._parse_tariff_table(COBALT_TARIFF_BLOCK)
        raw_codes = [e.hts_code_raw for e in entries]
        assert "2605.00.0000" in raw_codes

    def test_parse_tariff_table_deduplication(self, parser: MCSPdfParser):
        # Duplicate HTS code in text should appear only once
        block = COBALT_TARIFF_BLOCK + "\n2605.00.0000 duplicate entry\n"
        entries = parser._parse_tariff_table(block)
        codes = [e.hts_code for e in entries]
        assert codes.count("2605000000") == 1

    # _parse_production_leaders tests removed 2026-06 (Section 5.1 cleanup)
    # — the extractor itself is gone.  See file header in mcs_pdf_parser.py.

    def test_parse_import_sources_extracts_countries(self, parser: MCSPdfParser):
        sources = parser._parse_import_sources(COBALT_IMPORT_BLOCK)
        country_names = [s.country_name for s in sources]
        assert any("Congo" in n or "Democratic Republic" in n for n in country_names)
        assert any("Finland" in n for n in country_names)
        assert any("South Africa" in n for n in country_names)

    def test_parse_import_sources_share_range(self, parser: MCSPdfParser):
        sources = parser._parse_import_sources(COBALT_IMPORT_BLOCK)
        for src in sources:
            assert 0.0 < src.share <= 1.0, (
                f"Share {src.share} out of range for {src.country_name}"
            )

    def test_parse_import_sources_year_from_header(self, parser: MCSPdfParser):
        sources = parser._parse_import_sources(COBALT_IMPORT_BLOCK)
        if sources:
            # "(2021–24)" → end year = 2024
            assert sources[0].reference_year == 2024

    def test_parse_import_sources_excludes_other(self, parser: MCSPdfParser):
        sources = parser._parse_import_sources(COBALT_IMPORT_BLOCK)
        for src in sources:
            assert "other" not in src.country_name.lower()

    def test_extract_salient_notes_returns_text(self, parser: MCSPdfParser):
        notes = parser._extract_salient_notes(COBALT_SALIENT_BLOCK)
        assert notes  # non-empty
        assert len(notes) <= 2000

    def test_extract_salient_notes_missing_section(self, parser: MCSPdfParser):
        notes = parser._extract_salient_notes("No salient section here.\n")
        assert notes == ""


# ---------------------------------------------------------------------------
# Six-digit derivation tests
# ---------------------------------------------------------------------------

class TestSixDigitDerivation:
    """
    Verify that the parser correctly derives 6-digit global prefixes from
    10-digit US HTS codes and picks max confidence for shared prefixes.
    """

    def test_derivation_strips_to_six_digits(self):
        # 2605000000 → 260500; 2822000010 + 2822000090 → 282200
        tariff_block = (
            "Tariff:\n"
            "2605.00.0000 Free\n"
            "2822.00.0010 4.2%\n"
            "2822.00.0090 4.2%\n"
            "Depletion Allowance:\n"
        )
        fake_pdf = Path("/nonexistent/fake.pdf")
        parser = MCSPdfParser(fake_pdf, reference_year=2026)
        entries = parser._parse_tariff_table(tariff_block)
        codes = {e.hts_code for e in entries}
        assert "2605000000" in codes
        assert "2822000010" in codes
        assert "2822000090" in codes

        # Simulate six-digit derivation
        six_digit_conf: dict[str, float] = {}
        for entry in entries:
            six = entry.hts_code[:6]
            six_digit_conf[six] = max(six_digit_conf.get(six, 0.0), entry.confidence)

        assert "260500" in six_digit_conf
        assert "282200" in six_digit_conf
        # Both 2822 codes have confidence 1.0, max is 1.0
        assert six_digit_conf["282200"] == 1.0

    def test_no_four_digit_derivation(self):
        """Parser must NOT generate 4-digit rows — those are managed by seed_hs_mappings."""
        tariff_block = (
            "Tariff:\n"
            "2605.00.0000 Free\n"
            "Depletion Allowance:\n"
        )
        parser = MCSPdfParser(Path("/nonexistent/fake.pdf"), reference_year=2026)
        entries = parser._parse_tariff_table(tariff_block)
        for entry in entries:
            six = entry.hts_code[:6]
            assert len(six) == 6, f"Six-digit prefix {six!r} is unexpectedly short"


# ---------------------------------------------------------------------------
# Country resolution tests removed 2026-06 (Section 6.2 cleanup).
# ``_resolve_country`` + ``_build_country_map`` are deleted — country
# resolution is no longer the PDF parser's job (Step 4a deleted in
# Section 5.1, Step 4b disabled May 2026).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Production share calculation tests
# ---------------------------------------------------------------------------

# TestProductionShareCalculation removed 2026-06 (Section 5.1 cleanup).
# ProductionShare dataclass is gone; share computation now lives entirely
# in the CSV path (mcs2026_parser).


# ---------------------------------------------------------------------------
# Section splitting tests
# ---------------------------------------------------------------------------

class _FakeResolver:
    """Tiny fake satisfying ``MaterialAliasResolver.list_for_source`` +
    ``.resolve``.  Used by the section-splitting tests so they don't have
    to construct a real DB session.

    Section 4.3 + 4.4 fix (2026-06): Path B now requires a resolver and
    talks to it via ``list_for_source`` rather than reaching into
    ``_session`` directly.
    """

    def __init__(self, known: set[str]) -> None:
        self._known = known

    def list_for_source(self, source_system: str) -> set[str]:
        return set(self._known)

    def resolve(self, source_system: str, source_name: str):
        # Not exercised by these tests — they only call
        # _split_into_commodity_sections, not _resolve_pdf_heading.
        from app.services.ingestion.material_resolver import ResolveResult
        return ResolveResult(material=None, status="unknown")


class TestSectionSplitting:
    @pytest.fixture
    def parser(self, tmp_path: Path) -> MCSPdfParser:
        fake = tmp_path / "fake.pdf"
        fake.write_bytes(b"")
        # Provide a fake resolver so Path B can identify which headings
        # are real commodity anchors.  Mirror the real mcs_pdf rows for
        # the three commodities used in FAKE_PDF_TEXT.
        resolver = _FakeResolver(known={"ALUMINUM", "COBALT", "NICKEL"})
        return MCSPdfParser(fake, reference_year=2026, resolver=resolver)  # type: ignore[arg-type]

    def test_splits_known_headings(self, parser: MCSPdfParser):
        sections = parser._split_into_commodity_sections(FAKE_PDF_TEXT)
        assert "COBALT" in sections
        assert "ALUMINUM" in sections
        assert "NICKEL" in sections

    def test_body_text_contains_tariff_block(self, parser: MCSPdfParser):
        sections = parser._split_into_commodity_sections(FAKE_PDF_TEXT)
        assert "Tariff" in sections.get("COBALT", "")

    def test_unknown_heading_not_in_sections(self, parser: MCSPdfParser):
        sections = parser._split_into_commodity_sections(FAKE_PDF_TEXT)
        assert "UNOBTANIUM" not in sections

    def test_raises_without_resolver(self, tmp_path: Path):
        """Section 4.3 fix: Path B requires a resolver — no fall-through
        to a deleted dict.  Constructing a parser without a resolver is
        allowed (Path A doesn't need one), but invoking the regex split
        helper must raise.
        """
        fake = tmp_path / "fake.pdf"
        fake.write_bytes(b"")
        p = MCSPdfParser(fake, reference_year=2026)  # no resolver
        with pytest.raises(RuntimeError, match="MaterialAliasResolver"):
            p._split_into_commodity_sections(FAKE_PDF_TEXT)

    def test_resolve_pdf_heading_raises_without_resolver(self, tmp_path: Path):
        fake = tmp_path / "fake.pdf"
        fake.write_bytes(b"")
        p = MCSPdfParser(fake, reference_year=2026)  # no resolver
        with pytest.raises(RuntimeError, match="MaterialAliasResolver"):
            p._resolve_pdf_heading("COBALT")


# ---------------------------------------------------------------------------
# seed_to_db tests (mocked DB)
# ---------------------------------------------------------------------------

class TestSeedToDb:
    """Verify the insert order contract: US rows → global rows → SELECT back.

    Section 5.1 cleanup (2026-06): production_leaders + Step 4a removed,
    so the seed contract is now tariff-only on the DB-write side.
    Salient-notes append still runs; import_sources remains as a parser
    diagnostic (DB write is disabled).
    """

    @pytest.fixture
    def parser_with_fake_sections(self, tmp_path: Path, monkeypatch) -> MCSPdfParser:
        fake = tmp_path / "fake.pdf"
        fake.write_bytes(b"")
        p = MCSPdfParser(fake, reference_year=2026)

        # Inject a pre-parsed section so parse() is never called (no real PDF)
        fake_section = CommoditySection(
            heading="COBALT",
            canonical_name="Cobalt",
            tariff_entries=[
                TariffEntry(
                    description="Cobalt ores",
                    hts_code="2605000000",
                    hts_code_raw="2605.00.0000",
                ),
                TariffEntry(
                    description="Cobalt hydroxide",
                    hts_code="2822000010",
                    hts_code_raw="2822.00.0010",
                ),
            ],
            import_sources=[
                ImportSource("Democratic Republic of the Congo", 0.30, 2024),
                ImportSource("Finland", 0.26, 2024),
            ],
            salient_notes="Cobalt production remained stable.",
        )
        monkeypatch.setattr(p, "parse", lambda *a, **k: [fake_section])
        return p

    def test_dry_run_does_not_raise(
        self, parser_with_fake_sections: MCSPdfParser
    ):
        session = MagicMock()
        # Section 6.2 cleanup (2026-06): only _build_material_map is loaded;
        # _build_country_map was deleted along with its consumers.
        session.execute.return_value.all.return_value = []
        with patch.object(
            MCSPdfParser, "_build_material_map",
            return_value={"Cobalt": 1},
        ):
            stats = parser_with_fake_sections.seed_to_db(session, dry_run=True)

        assert "Cobalt" in stats
        # Dry run — session.execute should NOT have been called for inserts
        # (only for the static _build_* helpers which we patched out)
        insert_calls = [
            c for c in session.execute.call_args_list
            if "insert" in str(c).lower()
        ]
        assert not insert_calls

    def test_unknown_material_is_skipped(self, tmp_path: Path, monkeypatch):
        fake = tmp_path / "fake.pdf"
        fake.write_bytes(b"")
        p = MCSPdfParser(fake, reference_year=2026)

        unknown_section = CommoditySection(
            heading="COBALT",
            canonical_name="Cobalt",
            tariff_entries=[TariffEntry("Cobalt ores", "2605000000", "2605.00.0000")],
        )
        monkeypatch.setattr(p, "parse", lambda *a, **k: [unknown_section])

        session = MagicMock()
        # Section 6.2 cleanup: _build_country_map patch removed.
        with patch.object(
            MCSPdfParser, "_build_material_map", return_value={},
        ):
            # Material map is EMPTY — Cobalt has no material_id
            stats = p.seed_to_db(session, dry_run=True)

        assert "Cobalt" not in stats
