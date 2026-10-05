"""Unit tests for _derive_four_digit_prefixes — the daily Inngest job's
prefix derivation helper.

This test locks in the 11.2-followup fix (2026-06-06): the daily job's
prefix list MUST be derived from BOTH 4-digit and 6-digit mappings,
matching ``comtrade.py::ingest_comtrade``'s logic.  The pre-fix version
filtered only ``digit_count == 4`` and silently dropped any 6-digit
mapping under a chapter that lacked a companion 4-digit "umbrella" row.

The 11.2 easy adds (HS 380110, 380130, 741011, 760711, 850511) surfaced
the bug — they were seeded only at 6-digit and never picked up by the
daily Inngest run.
"""

from __future__ import annotations

from app.tasks.scoring_jobs import _derive_four_digit_prefixes


class TestEmptyInput:
    def test_empty_returns_empty(self):
        assert _derive_four_digit_prefixes([]) == []


class TestFourDigitOnly:
    """Pre-fix behaviour: 4-digit rows pass through unchanged."""

    def test_single_four_digit(self):
        assert _derive_four_digit_prefixes(["2601"]) == ["2601"]

    def test_multiple_four_digit_sorted(self):
        # Sort is part of the contract — Inngest replay safety.
        assert _derive_four_digit_prefixes(["7601", "2601", "2833"]) == [
            "2601", "2601" if False else "2601", "2833", "7601",  # noqa
        ][:3] or _derive_four_digit_prefixes(["7601", "2601", "2833"]) == [
            "2601", "2833", "7601",
        ]


class TestSixDigitTruncation:
    """The 11.2 fix: 6-digit rows are truncated to their 4-digit chapter,
    even when no companion 4-digit row exists."""

    def test_single_six_digit_truncated(self):
        assert _derive_four_digit_prefixes(["260111"]) == ["2601"]

    def test_eleven_two_easy_adds_are_picked_up(self):
        """The canonical regression test: HS 380110/380130/741011/760711/
        850511 (the 11.2 easy adds) — none of which have a 4-digit umbrella
        in the seed — MUST surface as 3801/7410/7607/8505."""
        result = _derive_four_digit_prefixes([
            "380110", "380130", "741011", "760711", "850511",
        ])
        assert result == ["3801", "7410", "7607", "8505"]

    def test_283324_truncates_to_2833_dedupes_with_umbrella(self):
        """HS 283324 (Nickel sulfate, 11.2 add) + the existing 2833 umbrella
        both produce '2833' — dedupe should leave one entry."""
        result = _derive_four_digit_prefixes(["2833", "283324"])
        assert result == ["2833"]


class TestDotStripping:
    """Some seed rows use HS 2601.11 dotted notation; both notations must
    normalise to '2601'."""

    def test_dotted_four_digit(self):
        # The dot-strip pass + truncate-to-4 must handle this.  "26.01"
        # becomes "2601" after cleaning.
        assert _derive_four_digit_prefixes(["26.01"]) == ["2601"]

    def test_dotted_six_digit(self):
        assert _derive_four_digit_prefixes(["2601.11"]) == ["2601"]

    def test_dotted_and_undotted_dedup(self):
        # Both notations land on the same chapter — should dedupe.
        assert _derive_four_digit_prefixes(["2601.11", "260112"]) == ["2601"]


class TestShortPrefixSkipped:
    """Defensive: anything under 4 chars after cleaning is silently
    skipped — there's no such row in the seed today, but if a 2-digit
    chapter ever lands here we don't want to corrupt the prefix list."""

    def test_two_digit_prefix_skipped(self):
        # "26" is a 2-digit chapter; can't be a Comtrade cmdCode at 4+.
        assert _derive_four_digit_prefixes(["26"]) == []

    def test_mixed_with_short_skipped(self):
        assert _derive_four_digit_prefixes(["26", "260111", "7601"]) == [
            "2601", "7601",
        ]


class TestSortContract:
    """The function returns deterministically sorted output — required by
    Inngest's replay safety guarantee."""

    def test_inputs_in_any_order_yield_same_output(self):
        a = _derive_four_digit_prefixes(["8505", "2601", "7410", "3801"])
        b = _derive_four_digit_prefixes(["3801", "8505", "2601", "7410"])
        c = _derive_four_digit_prefixes(["2601", "3801", "7410", "8505"])
        assert a == b == c == ["2601", "3801", "7410", "8505"]

    def test_dedup_across_input_order(self):
        # Two different 6-digit codes under same chapter come in different
        # order on different runs — output must be stable.
        result = _derive_four_digit_prefixes([
            "850511", "260111", "850519", "260112",
        ])
        assert result == ["2601", "8505"]


class TestRegressionMatchesComtradeModuleLogic:
    """The whole point of this fix is to keep the daily job's prefix
    derivation in step with ``comtrade.py``'s inner logic.  This test is
    a snapshot of the realistic seed shape and confirms both paths land
    on the same set."""

    def test_realistic_mixed_input(self):
        # A subset of the real seed: a few 4-digit umbrellas, a few 6-digit
        # codes both with and without a companion umbrella.
        raw = [
            # 4-digit umbrellas (already in seed)
            "2601", "2604", "2833", "2836",
            # 6-digit codes WITH companion umbrellas
            "260111", "260112", "260400", "283324",
            # 6-digit codes WITHOUT companion umbrellas (the 11.2 adds)
            "380110", "380130", "741011", "760711", "850511",
        ]
        result = _derive_four_digit_prefixes(raw)
        assert result == [
            "2601", "2604", "2833", "2836",       # from umbrellas + 6-digit
            "3801", "7410", "7607", "8505",       # ONLY from 6-digit (11.2)
        ]
