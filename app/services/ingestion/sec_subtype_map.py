"""SEC filing → (event_subtype, risk_category, severity_bump) mapping.

Single source of truth for how the SEC ingester translates a filing's
``form`` + ``items[]`` into the typed columns on ``RiskEvent``.  The
mapping is also imported by ``evidence_aggregator.derive_financial_inputs``
to decide which subtypes trigger the leverage / liquidity bonuses inside
the financial-pressure pillar.

Two-axis mapping
----------------
Each SEC filing resolves to a tuple of:

  * **event_subtype**  — granular identifier on ``RiskEvent.event_subtype``
    (migration 040 typed column).  Drives bucketing in
    ``evidence_aggregator`` (leverage_warning_bonus, liquidity_stress_bonus)
    and downstream filtering.  Replaces the pre-2026-05-23 hardcoded
    ``"FINANCIAL_PRESSURE"`` value that conflated every SEC filing.

  * **risk_category**  — single ``RiskCategory`` enum value that lands in
    ``RiskEvent.risk_categories_json``.  Drives which pillar's evidence
    query picks up the event.  Distinct from subtype: subtype is "what
    kind of event", category is "which pillar consumes it".

  * **severity_bump** — added to a small form-base severity to produce
    final ``RiskEvent.severity_score``.  Reflects ordinal severity of
    the event type (bankruptcy max, routine disclosure min).  Calibrated
    against the methodology handbook's existing ``RiskEvent.severity_score``
    bands; tuned numbers should land in evidence_aggregator output ranges
    the financial-pressure pillar already expects.

8-K item → subtype priority
---------------------------
When an 8-K carries multiple items (common — e.g. "1.01,9.01" or
"2.04,8.01,9.01") we pick the **most-severe** item per ``ITEM_8K_MAP``
ordering (highest ``severity_bump`` first; ties broken by item code
ascending).  All items remain in ``SourceDocument.metadata_json["items"]``
for audit, but only the dominant one shapes the RiskEvent payload.

Coverage notes
--------------
ITEM_8K_MAP covers the 8-K items SEC uses today.  Items that don't appear
(rare or deprecated 1.04, 3.04, 6.x, 9.02+) fall through to the FORM_MAP
default for that form — typically ``FILING_INDEX`` at base 8-K severity.

FORM_MAP covers the forms in active use by the curated CIK list.  Other
forms (S-1, S-3, S-4, SC 13D/G, DEF 14A, etc.) fall through to a
generic default (``FILING_INDEX``, ``FINANCIAL_PRESSURE``, low severity).

Aggregator wiring
-----------------
``LEVERAGE_SUBTYPES`` and ``LIQUIDITY_SUBTYPES`` are imported by
``evidence_aggregator.derive_financial_inputs`` to expand its bonus
criteria beyond the original aspirational vocabulary
(LEVERAGE_WARNING, COVENANT_STRESS, GOING_CONCERN, CASH_RUNWAY,
CAPEX_CUT) — those were placeholders no ingester wrote.  The SEC
ingester is the first source emitting the granular subtypes the
aggregator expected.
"""

from __future__ import annotations

from app.constants import RiskCategory


# 8-K item code → (event_subtype, risk_category, severity_bump).
# severity_bump is the increment added to the form-base severity in
# RiskEventDraft.severity_score.  Ordered conceptually highest → lowest
# severity; the actual selection uses ``severity_bump`` to break ties so
# adding new items doesn't depend on dict insertion order.
ITEM_8K_MAP: dict[str, tuple[str, str, float]] = {
    # ── Financial distress (highest severity) ─────────────────────────
    "1.03": ("BANKRUPTCY",              RiskCategory.FINANCIAL_PRESSURE.value, 0.55),
    "2.04": ("DEBT_ACCELERATION",       RiskCategory.FINANCIAL_PRESSURE.value, 0.40),
    "4.02": ("FINANCIAL_RESTATEMENT",   RiskCategory.FINANCIAL_PRESSURE.value, 0.35),
    "2.06": ("MATERIAL_IMPAIRMENT",     RiskCategory.FINANCIAL_PRESSURE.value, 0.30),
    "3.01": ("DELISTING_NOTICE",        RiskCategory.FINANCIAL_PRESSURE.value, 0.30),
    "4.01": ("AUDITOR_CHANGE",          RiskCategory.FINANCIAL_PRESSURE.value, 0.20),
    "2.05": ("RESTRUCTURING",           RiskCategory.FINANCIAL_PRESSURE.value, 0.20),
    "2.03": ("DEBT_OBLIGATION",         RiskCategory.FINANCIAL_PRESSURE.value, 0.10),

    # ── Operational signals ────────────────────────────────────────────
    "1.05": ("CYBERSECURITY_INCIDENT",  RiskCategory.OPERATIONAL.value,         0.25),
    "1.02": ("MATERIAL_AGREEMENT_TERMINATION", RiskCategory.OPERATIONAL.value,  0.20),
    "1.01": ("MATERIAL_AGREEMENT",      RiskCategory.OPERATIONAL.value,         0.15),
    "2.01": ("ACQUISITION",             RiskCategory.OPERATIONAL.value,         0.10),
    "5.02": ("GOVERNANCE_CHANGE",       RiskCategory.OPERATIONAL.value,         0.05),
    "5.03": ("CORPORATE_GOVERNANCE",    RiskCategory.OPERATIONAL.value,         0.00),
    "5.07": ("SHAREHOLDER_VOTE",        RiskCategory.OPERATIONAL.value,         0.00),

    # ── Lower-signal financial events ─────────────────────────────────
    "2.02": ("EARNINGS_RELEASE",        RiskCategory.FINANCIAL_PRESSURE.value, 0.00),
    "3.02": ("EQUITY_ISSUANCE",         RiskCategory.FINANCIAL_PRESSURE.value, 0.00),
    "3.03": ("SECURITY_MODIFICATION",   RiskCategory.FINANCIAL_PRESSURE.value, 0.00),

    # ── Lowest-signal "noise" items ───────────────────────────────────
    "7.01": ("REGULATION_FD_DISCLOSURE", RiskCategory.FINANCIAL_PRESSURE.value, 0.00),
    "8.01": ("OTHER_EVENT",              RiskCategory.FINANCIAL_PRESSURE.value, 0.00),
    "9.01": ("ATTACHED_EXHIBITS",        RiskCategory.FINANCIAL_PRESSURE.value, 0.00),
}


# Form code → (event_subtype, risk_category, base_severity).
# Used when the form has no items (10-K/10-Q/20-F) or the items don't
# resolve in ITEM_8K_MAP.  ``base_severity`` here is the starting point
# for the form (vs. ``severity_bump`` for items, which is added to a
# form base).  NT 10-K / NT 10-Q (late-filing notifications) are higher
# signal than the regular form because they indicate the filer couldn't
# file on time — a leading indicator of distress.
FORM_MAP: dict[str, tuple[str, str, float]] = {
    "10-K":    ("ANNUAL_REPORT",                  RiskCategory.FINANCIAL_PRESSURE.value, 0.20),
    "10-K/A":  ("ANNUAL_REPORT_AMENDMENT",        RiskCategory.FINANCIAL_PRESSURE.value, 0.35),
    "10-Q":    ("QUARTERLY_REPORT",               RiskCategory.FINANCIAL_PRESSURE.value, 0.15),
    "10-Q/A":  ("QUARTERLY_REPORT_AMENDMENT",     RiskCategory.FINANCIAL_PRESSURE.value, 0.30),
    "20-F":    ("FOREIGN_ANNUAL_REPORT",          RiskCategory.FINANCIAL_PRESSURE.value, 0.20),
    "20-F/A":  ("FOREIGN_ANNUAL_REPORT_AMENDMENT", RiskCategory.FINANCIAL_PRESSURE.value, 0.35),
    "6-K":     ("FOREIGN_CURRENT_REPORT",         RiskCategory.FINANCIAL_PRESSURE.value, 0.15),
    "NT 10-K": ("LATE_FILING_NOTIFICATION",       RiskCategory.FINANCIAL_PRESSURE.value, 0.45),
    "NT 10-Q": ("LATE_FILING_NOTIFICATION",       RiskCategory.FINANCIAL_PRESSURE.value, 0.45),
    "8-K":     ("FILING_INDEX",                   RiskCategory.FINANCIAL_PRESSURE.value, 0.10),
    "11-K":    ("EMPLOYEE_BENEFIT_REPORT",        RiskCategory.FINANCIAL_PRESSURE.value, 0.05),
    "S-1":     ("REGISTRATION_STATEMENT",         RiskCategory.FINANCIAL_PRESSURE.value, 0.10),
    "S-3":     ("REGISTRATION_STATEMENT",         RiskCategory.FINANCIAL_PRESSURE.value, 0.10),
    "DEF 14A": ("PROXY_STATEMENT",                RiskCategory.OPERATIONAL.value,         0.05),
}

# Default for forms not in FORM_MAP — keeps the ingester safe against
# new SEC form types appearing in submissions JSON without code changes.
_DEFAULT_MAPPING: tuple[str, str, float] = (
    "FILING_INDEX",
    RiskCategory.FINANCIAL_PRESSURE.value,
    0.10,
)

# Subtype sets consumed by evidence_aggregator.derive_financial_inputs.
# Source-of-truth list lives here so adding a new SEC subtype to the
# leverage/liquidity bucket means one edit in one file.

LEVERAGE_SUBTYPES: frozenset[str] = frozenset({
    # New SEC subtypes that signal leverage / debt issues
    "DEBT_ACCELERATION",
    "DEBT_OBLIGATION",
    "FINANCIAL_RESTATEMENT",
    "ANNUAL_REPORT_AMENDMENT",
    "QUARTERLY_REPORT_AMENDMENT",
    "FOREIGN_ANNUAL_REPORT_AMENDMENT",
    # Aspirational vocabulary kept for compatibility with non-SEC ingesters
    # that may eventually emit these
    "LEVERAGE_WARNING",
    "COVENANT_STRESS",
})

LIQUIDITY_SUBTYPES: frozenset[str] = frozenset({
    # New SEC subtypes that signal liquidity / going-concern issues
    "BANKRUPTCY",
    "MATERIAL_IMPAIRMENT",
    "DELISTING_NOTICE",
    "LATE_FILING_NOTIFICATION",
    "AUDITOR_CHANGE",
    "RESTRUCTURING",
    # Aspirational vocabulary
    "GOING_CONCERN",
    "CASH_RUNWAY",
    "CAPEX_CUT",
})


def map_sec_filing(
    form: str | None,
    items: list[str] | None = None,
) -> tuple[str, str, float]:
    """Resolve a SEC filing to (event_subtype, risk_category, severity_bump).

    Algorithm:
      1. If form is 8-K or 6-K and items are present, pick the highest-
         severity item from ITEM_8K_MAP (ties → item code ascending).
      2. Else look up form in FORM_MAP.
      3. Else fall back to _DEFAULT_MAPPING.

    Robust to ``None`` and case variation.  Returns a tuple suitable for
    plugging directly into RiskEventDraft.

    Args:
        form:  SEC form code (e.g. "8-K", "10-K", "20-F").  Case-insensitive.
        items: 8-K item codes parsed from filings.recent.items (e.g.
               ["1.01", "9.01"]).  Empty / None for non-8-K forms.

    Returns:
        (event_subtype, risk_category_value, severity_bump) tuple.
    """
    form_upper = (form or "").upper().strip()
    items = items or []

    # 8-K / 6-K with items → pick highest-severity item
    if items and form_upper in ("8-K", "6-K"):
        matched = [
            (item, ITEM_8K_MAP[item])
            for item in items
            if item in ITEM_8K_MAP
        ]
        if matched:
            # Sort: highest severity_bump first, then item code ascending
            matched.sort(key=lambda pair: (-pair[1][2], pair[0]))
            _, mapping = matched[0]
            return mapping

    # Form-level lookup
    if form_upper in FORM_MAP:
        return FORM_MAP[form_upper]

    # Unknown form — log via caller; return safe default
    return _DEFAULT_MAPPING
