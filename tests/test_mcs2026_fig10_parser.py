"""Tests for app/services/ingestion/seeds/mcs2026_fig10_parser.py.

Covers _parse_pct sentinel + bound handling and the parse_mcs2026_fig10_csv
encoding fallback / row-skip behaviour.  Uses temp files for the I/O
tests so they run without touching the real Fig 10 CSV.
"""

from __future__ import annotations

import csv
import os
import tempfile
from pathlib import Path

import pytest

from app.services.ingestion.seeds.mcs2026_fig10_parser import (
    _parse_pct,
    parse_mcs2026_fig10_csv,
)
from app.services.ingestion.seeds.mcs2026_parser import _NO_DATA_SENTINELS


# ─────────────────────────────────────────────────────────────────────────
# _parse_pct — sentinels, bounds, edge cases
# ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Normal numbers
        ("0",      0.0),
        ("7",      0.07),
        ("-24",   -0.24),
        ("270",    2.70),    # Beryllium-style legitimate >100% growth
        ("-100",  -1.0),     # full price drop
        # Blank / None
        ("",       None),
        ("   ",    None),
        (None,     None),
        # Bounded estimates (direction-lossy)
        (">95",    0.95),
        ("<10",    0.10),
        # Junk
        ("foo",    None),
        ("1.5x",   None),
    ],
)
def test_parse_pct_numeric_and_bound_patterns(raw, expected):
    assert _parse_pct(raw) == expected


@pytest.mark.parametrize("sentinel", sorted(_NO_DATA_SENTINELS))
def test_parse_pct_returns_none_for_shared_sentinels(sentinel):
    """Issue 9.1: _parse_pct uses the same no-data sentinel set as the
    main commodity parser (W, NA, E, s, XX, em-dash, en-dash, hyphen)."""
    assert _parse_pct(sentinel) is None


def test_parse_pct_negative_number_not_confused_with_hyphen_sentinel():
    """Bare '-' is a sentinel (no data); '-24' is a real negative number.
    Sentinel check uses exact membership so the negative-number case
    parses correctly."""
    assert _parse_pct("-") is None
    assert _parse_pct("-24") == -0.24


# ─────────────────────────────────────────────────────────────────────────
# parse_mcs2026_fig10_csv — I/O behavior
# ─────────────────────────────────────────────────────────────────────────


def _write_csv(path: str, rows: list[dict], encoding: str = "utf-8-sig"):
    """Write a minimal Fig 10-format CSV at the given path/encoding."""
    fieldnames = ["critical_mineral_priced", "PCH_2024_2025", "CAGR_2021_2025", "Notes"]
    with open(path, "w", encoding=encoding, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_parse_fig10_basic_output_contract(tmp_path):
    path = tmp_path / "fig10.csv"
    _write_csv(str(path), [
        {"critical_mineral_priced": "Aluminum, bauxite",
         "PCH_2024_2025": "3", "CAGR_2021_2025": "1", "Notes": "ok"},
        {"critical_mineral_priced": "Lithium, battery-grade lithium carbonate",
         "PCH_2024_2025": "-24", "CAGR_2021_2025": "10", "Notes": ""},
    ])
    records = parse_mcs2026_fig10_csv(path)
    assert len(records) == 2

    # Output contract — every field present, correct types
    expected_keys = {
        "source_system", "source_name",
        "price_yoy_pct", "price_cagr_5yr_pct",
        "raw_pch", "raw_cagr",
    }
    for rec in records:
        assert set(rec.keys()) == expected_keys
        assert rec["source_system"] == "fig10_prices"

    # Values
    assert records[0]["source_name"] == "Aluminum, bauxite"
    assert records[0]["price_yoy_pct"] == 0.03
    assert records[0]["price_cagr_5yr_pct"] == 0.01
    assert records[0]["raw_pch"] == "3"

    assert records[1]["source_name"] == "Lithium, battery-grade lithium carbonate"
    assert records[1]["price_yoy_pct"] == -0.24


def test_parse_fig10_skips_rows_with_no_signal(tmp_path):
    """Issue 9.5: row with both metrics None/sentinel is dropped silently
    (but logged); rows with at least one metric survive."""
    path = tmp_path / "fig10.csv"
    _write_csv(str(path), [
        {"critical_mineral_priced": "HasYoY",
         "PCH_2024_2025": "5", "CAGR_2021_2025": "", "Notes": ""},
        {"critical_mineral_priced": "BothEmpty",
         "PCH_2024_2025": "", "CAGR_2021_2025": "", "Notes": ""},
        {"critical_mineral_priced": "BothSentinel",
         "PCH_2024_2025": "W", "CAGR_2021_2025": "NA", "Notes": ""},
        {"critical_mineral_priced": "HasCAGR",
         "PCH_2024_2025": "", "CAGR_2021_2025": "12", "Notes": ""},
    ])
    records = parse_mcs2026_fig10_csv(path)
    names = [r["source_name"] for r in records]
    assert names == ["HasYoY", "HasCAGR"]


def test_parse_fig10_skips_rows_with_empty_name(tmp_path):
    path = tmp_path / "fig10.csv"
    _write_csv(str(path), [
        {"critical_mineral_priced": "Has name",
         "PCH_2024_2025": "5", "CAGR_2021_2025": "10", "Notes": ""},
        {"critical_mineral_priced": "",
         "PCH_2024_2025": "5", "CAGR_2021_2025": "10", "Notes": ""},
        {"critical_mineral_priced": "   ",
         "PCH_2024_2025": "5", "CAGR_2021_2025": "10", "Notes": ""},
    ])
    records = parse_mcs2026_fig10_csv(path)
    assert [r["source_name"] for r in records] == ["Has name"]


def test_parse_fig10_strips_trailing_whitespace_from_name(tmp_path):
    path = tmp_path / "fig10.csv"
    _write_csv(str(path), [
        {"critical_mineral_priced": "Nickel ",   # trailing space
         "PCH_2024_2025": "7", "CAGR_2021_2025": "3", "Notes": ""},
    ])
    records = parse_mcs2026_fig10_csv(path)
    assert records[0]["source_name"] == "Nickel"


def test_parse_fig10_cp1252_encoding_fallback(tmp_path):
    """Issue 9.6: when file is cp1252-encoded (not utf-8-sig), fall back
    to cp1252 instead of crashing on UnicodeDecodeError."""
    path = tmp_path / "fig10.csv"
    # Write a cp1252-encoded file with an em-dash that's NOT valid utf-8
    # at byte position (em-dash 0x97 in cp1252 vs. 0xE2 0x80 0x94 in utf-8).
    _write_csv(str(path), [
        {"critical_mineral_priced": "Cobalt — primary",  # contains em-dash
         "PCH_2024_2025": "5", "CAGR_2021_2025": "3", "Notes": ""},
    ], encoding="cp1252")
    # Parser should succeed via fallback
    records = parse_mcs2026_fig10_csv(path)
    assert len(records) == 1
    assert "Cobalt" in records[0]["source_name"]


def test_parse_fig10_utf8_sig_preserves_first_column(tmp_path):
    """utf-8-sig must strip the BOM cleanly so the first column header
    matches the expected 'critical_mineral_priced' name (no BOM bytes
    leaking into the dict key)."""
    path = tmp_path / "fig10.csv"
    _write_csv(str(path), [
        {"critical_mineral_priced": "Aluminum, bauxite",
         "PCH_2024_2025": "3", "CAGR_2021_2025": "1", "Notes": ""},
    ])
    records = parse_mcs2026_fig10_csv(path)
    assert records[0]["source_name"] == "Aluminum, bauxite"
