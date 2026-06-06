"""
Convert trade_flows data into GEOPOLITICAL_TRADE RiskEvent rows.

The UN Comtrade ingestion writes raw export volumes into trade_flows, but the
scoring engine reads risk_events (joined through risk_event_companies). This
module bridges that gap by deriving three signal types:

  TRADE_CONCENTRATION
    Raised when a single country accounts for ≥60% of total tracked reporter
    exports for a given material in a given year. Feeds trade_volatility in
    the Material Concentration pillar and country_concentration in the
    Geopolitical pillar.

  EXPORT_DECLINE
    Raised when a reporter country's exports of a material fall ≥25% YoY
    (9.2.B raised the threshold from 20% to 25% to reduce false positives
    from cyclical commodity variation).  Uses event_subtype =
    "EXPORT_DECLINE" — 9.2.E renamed from "EXPORT_RESTRICTION" to make the
    semantic distinction explicit: this signal infers a decline from trade
    volumes but does NOT directly observe the cause.  Direct policy-driven
    sources (gta.py, opensanctions.py, ingest_federal_register.py) continue
    to emit "EXPORT_RESTRICTION" for genuinely-observed restrictions.

    Scoring routing (post-9.3 audit):
      * NOT in ``export_restriction_exposure`` (Geopolitical pillar
        sub-input) — that's driven only by directly-observed
        EXPORT_RESTRICTION events from gta/opensanctions/federal_register.
      * NOT in HS-node ``tariff_exposure`` sub-input — that's policy-only
        (uses ``hs_node_scorer._EXPORT_SUBTYPES`` / ``_TARIFF_SUBTYPES``,
        neither of which include EXPORT_DECLINE).
      * DOES feed ``trade_volatility`` (Material Concentration pillar)
        via the broad ``risk_categories_json=[GEOPOLITICAL_TRADE]`` tag —
        which is set on every event this module emits.
      * DOES feed ``country_concentration`` (Geopolitical pillar's
        geo_trade_events branch) for the same category reason.

    The (lower) ``confidence_score`` weights these contributions down in
    ``compute_event_impact``, so the practical scoring impact is
    proportionally reduced versus directly-observed policy events at
    higher confidence.  A future methodology pass may strip the broad
    category tag entirely (making these events truly informational) or
    add a dedicated sub-input — both deferred until partner has Phase 1
    output to review.

    Only computed when we have data for both years AND both year groups
    pass the noise floor.

  IMPORT_DECLINE
    Raised when a consuming country's imports of a material fall ≥25% YoY.
    Uses event_subtype = "IMPORT_DECLINE" — 9.3.B renamed from
    "IMPORT_DISRUPTION".  Reasoning: gta.py emits IMPORT_DISRUPTION for
    directly-observed import tariff/quota/ban policies, which feed
    hs_node_scorer._TARIFF_SUBTYPES → tariff_exposure sub-input.  Our
    volume-decline-inferred signals are NOT tariff observations; sharing
    the IMPORT_DISRUPTION subtype with GTA was a pre-existing scoring
    bug (volume noise contaminating tariff_exposure).  The rename fixes
    it: IMPORT_DECLINE is NOT in _TARIFF_SUBTYPES.

    Confidence: IMPORT_DECLINE uses confidence_score = 0.55 by default
    (vs EXPORT_DECLINE's 0.75).  See _IMPORT_DECLINE_CONFIDENCE_SCORE
    docstring for the reasoning (more plausible causes per drop + the
    downstream-echo problem when a real upstream supply shock produces
    both an EXPORT_DECLINE and an IMPORT_DECLINE for the same cause).
    0.55 is deliberately below the 0.60 high-severity confidence floor
    so IMPORT_DECLINE events never get the high-severity boost.

    Scoring routing (post-9.3 audit):
      * NOT in ``tariff_exposure`` (HS-node sub-input) — that's driven
        only by directly-observed IMPORT_DISRUPTION events from gta.py,
        which are import tariff/quota/ban policies.  This rename is
        the core 9.3 bug fix: previously, volume-decline-inferred
        signals were contaminating ``tariff_exposure``.
      * NOT in ``export_restriction_exposure`` — not relevant for
        import-side signals anyway.
      * DOES feed ``trade_volatility`` (Material Concentration pillar)
        and ``country_concentration`` (Geopolitical pillar) via the
        broad ``risk_categories_json=[GEOPOLITICAL_TRADE]`` tag.

    The lower ``confidence_score`` (0.55) weights these contributions
    down more aggressively than EXPORT_DECLINE's 0.75.  The practical
    impact is that catastrophic-severity (1.0) IMPORT_DECLINE events
    contribute meaningfully less to ``trade_volatility`` than
    same-magnitude EXPORT_DECLINE events.

Company linkage (risk_event_companies):
  Companies are linked via company_material_exposures: any company with
  source_geography matching the signal country and material_id matching
  the signal material is linked with relevance_score = 0.85.

Idempotency (parameter-stable UPSERT — 2026-05-11):
  content_hash is computed from a stable key
  ``f"{event_subtype}|material_id={mid}|country={cc}|year={yr}"`` rather
  than from the human-readable title.  Re-running this module against the
  same trade_flows data is a no-op on the existence check, but when the
  underlying calculation changes (e.g. confidence weighting in
  ``_get_annual_totals`` shifts country shares), the existing row is
  UPDATEd in place — title/severity/summary/metadata are rewritten,
  material + HS junctions are deleted and re-inserted, company junctions
  are left alone (partial-rewrite per audit decision).

  Result-dict counters split inserts from updates::

      concentration_events           # new TRADE_CONCENTRATION rows
      concentration_events_updated   # existing rows updated in place
      export_drop_events             # new EXPORT_RESTRICTION rows
      export_drop_events_updated
      import_drop_events             # new IMPORT_DISRUPTION rows
      import_drop_events_updated

  ``skipped_existing`` is retained for backwards-compat but always 0 under
  UPSERT semantics — what would have been a skip is now an update.

Severity calibration:
  TRADE_CONCENTRATION  (post-9.1.C, 2026-06)
    60–69%  → 0.55   (elevated — diversification advisable)
    70–79%  → 0.70   (high — single supplier dominance)
    80–89%  → 0.82   (very high — critical vulnerability)
    90–94%  → 0.90   (extreme — near-monopoly supply)
    ≥95%    → 1.00   (effective monopoly — was capped at 0.90 pre-9.1.C
                      with no documented rationale; investigation against
                      compute_event_impact confirmed severity is a direct
                      linear multiplier so the cap was costing ~11% of
                      max impact for true-monopoly events)

  EXPORT_DROP
    20–29%  → 0.50   (moderate disruption signal)
    30–39%  → 0.65   (significant — possible restriction)
    40–49%  → 0.78   (severe)
    ≥50%    → 0.88   (critical)

Data-quality flags (9.1.A, 2026-06):
  TRADE_CONCENTRATION events tag ``data_coverage_warning=True`` in
  ``metadata_json`` and reduce ``confidence_score`` from 0.75 to 0.45
  when the (material, year) group has fewer than 3 tracked reporters OR
  a tracked world_total under USD 5M.  This is a soft guard — events
  still fire so partner sees the signal, but downstream scoring weights
  them lower.  The 0.45 confidence is deliberately under the 0.60 floor
  in ``compute_effective_confidence`` so thin-data events do not benefit
  from the high-severity confidence boost.

Concentration structure metadata (9.1.B, 2026-06):
  Every TRADE_CONCENTRATION event carries an ``hhi`` field in
  ``metadata_json`` (0–10000 scale, Herfindahl-Hirschman Index across
  ALL reporting countries for the (material, year) group).  HHI is
  metadata-only today and does NOT change the firing logic — it lets
  partner sanity-check whether a 60% top-share is "60% with diversified
  rest" (HHI ~3800) or "60% with one concentrated competitor" (HHI
  ~4800).  Phase 1.5 may migrate severity to be HHI-derived rather than
  top-share-derived; the metadata trail makes that change
  backwards-compatible.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.constants import RiskCategory
from app.models.company import CompanyMaterialExposure
from app.services.ingestion import feature_flags
from app.models.regulatory import (
    RiskEvent,
    RiskEventCompany,
    RiskEventHsMapping,
    RiskEventMaterial,
)
from app.models.supply import TradeFlow

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# 9.7 (2026-06) — Partner=WLD constraint
# ---------------------------------------------------------------------------
# All trade signal aggregations in this module filter
# ``trade_flows.partner_country == 'WLD'`` (world aggregate).  The
# Comtrade ingester (``comtrade.fetch_annual_exports``) calls the API
# with ``partnerCode=0`` (the world-aggregate sentinel), so every
# Comtrade row's partner_country lands as 'WLD' — bilateral flows
# (e.g. "CN → US specifically") are NOT ingested today and would not
# be visible to this module even if they were.
#
# Phase 2 buyer-region scoping (see task #86) requires bilateral
# trade data so a US-buyer view can weight by US's import shares
# from each source country.  When that work lands, two things will
# change here:
#   1. The WLD filter below becomes ``partner_country IS NOT NULL``
#      (i.e. include both WLD aggregates AND bilateral rows).
#   2. The signal-derivation loops gain a per-buyer-region pass that
#      groups by (material_id, buyer_region, source_country, year)
#      rather than the current (material_id, reporter_country, year).
# Until then, every signal this module emits is implicitly scoped to
# "tracked reporter exports/imports to/from the world" — NOT to
# specific bilateral relationships.

# ---------------------------------------------------------------------------
# 9.9 (2026-06) — Event subtype taxonomy
# ---------------------------------------------------------------------------
# After the 9.2.E + 9.3.B renames, the complete taxonomy of GEOPOLITICAL_TRADE
# event subtypes the platform recognises is:
#
#   Subtype                Emitter(s)                            Scoring routing
#   ──────────────────────────────────────────────────────────────────────────
#   TRADE_CONCENTRATION    trade_signal_builder                  trade_volatility,
#                          (this module)                         country_concentration
#                                                                (via GEOPOLITICAL_TRADE
#                                                                category tag)
#
#   EXPORT_RESTRICTION     gta.py (export tax/quota/ban),        export_restriction_exposure
#                          opensanctions.py (sanctions),         (via event_subtype match
#                          ingest_federal_register.py            in evidence_aggregator +
#                                                                hs_node_scorer)
#
#   EXPORT_DECLINE         trade_signal_builder (this module)    trade_volatility,
#                                                                country_concentration
#                                                                (NOT export_restriction_exposure
#                                                                — 9.3 symmetric decision)
#
#   IMPORT_DISRUPTION      gta.py (import tariff/quota/ban)      tariff_exposure
#                                                                (via _TARIFF_SUBTYPES in
#                                                                hs_node_scorer)
#
#   IMPORT_DECLINE         trade_signal_builder (this module)    trade_volatility,
#                                                                country_concentration
#                                                                (NOT tariff_exposure
#                                                                — 9.3 bug fix)
#
# Key invariant: subtypes ending in "_DECLINE" are volume-INFERRED;
# subtypes ending in "_RESTRICTION" or "_DISRUPTION" are policy-OBSERVED.
# The naming distinction matters because the policy-keyed scoring sub-inputs
# (export_restriction_exposure, tariff_exposure) should only fire from
# directly-observed events, not from volume inferences.

# ---------------------------------------------------------------------------
# 9.10 (2026-06) — Named match_reason constants
# ---------------------------------------------------------------------------
# Hoisted out of inline string literals so any consumer that filters or
# audits by match_reason has a single source of truth.  Values match the
# pre-9.10 inline strings — no migration needed.

# RiskEventMaterial junction: trade flow's HS code resolved to this material.
_MATCH_REASON_MATERIAL = "hs_code"

# RiskEventHsMapping junction: HS code attributed via trade flow lookup.
_MATCH_REASON_HS_MAPPING = "trade_flow_match"

# RiskEventCompany junction: company sources this material from this country.
# Name retained as "material_hs" for backwards compatibility with the
# pre-9.10 inline string; semantically the match is (material × source
# country) so a Phase 1.5 rename to "material_geography" would be more
# accurate but requires a coordinated data update.
_MATCH_REASON_COMPANY = "material_hs"


# Minimum reporter share to raise a TRADE_CONCENTRATION event.
_CONCENTRATION_THRESHOLD = 0.60

# Minimum YoY export decline to raise an EXPORT_DECLINE / IMPORT_DROP event.
# 9.2.B fix (2026-06): raised from 0.20 to 0.25 to align with the trade-shock
# literature (IMF & academic trade economics commonly use 25-30% as the
# "unusual decline" threshold).  Commodity exports routinely swing 15-25%
# YoY from cyclical factors (price cycles, demand shifts, weather affecting
# mines) — 0.20 catches genuine shocks but also catches normal volatility,
# raising the false-positive rate.  Calibration tradeoff: loses some early-
# warning sensitivity to genuine 20-25% restrictions in exchange for fewer
# false positives from cyclical variation.  The first severity bucket's
# range moves from [0.20, 0.30) to [0.25, 0.30) accordingly.
_DROP_THRESHOLD = 0.25

# ---------------------------------------------------------------------------
# 9.4 (2026-06) — Per-country vs per-group noise floors split.
# ---------------------------------------------------------------------------
# Pre-9.4 there was a single ``_MIN_TRADE_VALUE_USD = $1M`` used both as
# a per-country flow floor AND a per-group (world-total) meaningfulness
# floor.  The constant served two semantically different purposes:
#
#   * Per-country: "is this country's flow large enough to be real
#     signal vs. trace data?"  $1M is roughly right for any tracked
#     battery material — below this is essentially noise.
#
#   * Per-group:   "is the entire (material, year) group large enough
#     to compute meaningful concentration on?"  $1M aggregate world
#     trade is very small; almost any battery material's tracked
#     trade exceeds $5M with reasonable coverage.
#
# 9.4 split: per-country stays at $1M; per-group bumps to $5M.  This
# eliminates a band of borderline groups (between $1M and $5M total
# trade) that previously fired with coverage_warning=True — the new
# behavior is to skip them entirely.  Defensible because a $5M total
# trade group is genuinely too thin to produce reliable concentration
# signals; firing with a "warning" was relying on downstream weighting
# we couldn't guarantee.

# Per-country flow noise floor.  Flows below this are trace and
# contribute nothing meaningful to share or drop calculations.  Uniform
# across all materials in Phase 1; per-material overrides tracked as a
# Phase 1.5 candidate.
_MIN_PER_COUNTRY_TRADE_USD = 1_000_000  # $1M

# Per-group (world_total) hard floor.  Groups with less tracked global
# trade than this are skipped entirely (no TRADE_CONCENTRATION,
# EXPORT_DECLINE, or IMPORT_DECLINE events fire for them).  9.4 bumped
# from $1M to $5M to align with the existing coverage-warning floor —
# the previous "<$5M fires with warning" band collapsed into the hard
# skip.
_MIN_WORLD_TOTAL_USD = 5_000_000  # $5M

# Backwards-compat alias kept for any external caller that still reads
# the pre-9.4 name.  Reads as the per-country floor since that's the
# semantically more common usage (drop calculations key off it on the
# per-country side).  Group-level callsites now use
# ``_MIN_WORLD_TOTAL_USD`` explicitly.
_MIN_TRADE_VALUE_USD = _MIN_PER_COUNTRY_TRADE_USD

# ---------------------------------------------------------------------------
# 9.6 (2026-06) — Company relevance derived from exposure data
# ---------------------------------------------------------------------------
# Pre-9.6 every company linked via material + geography match received a
# hardcoded ``_COMPANY_RELEVANCE = 0.85`` — which mapped through
# ``event_impact.relevance_score_to_multiplier`` to a 1.21x boost
# regardless of how exposed the company actually was.
#
# CompanyMaterialExposure carries two partner-curated fields the linker
# was ignoring: ``exposure_score`` (0.0-1.0, how exposed the company is
# to this material at this supply-chain stage) and ``data_confidence``
# (0.0-1.0, partner confidence in the exposure assessment).  9.6 wires
# those into the relevance calculation so a company with low real-world
# exposure or low-confidence data no longer gets a 1.21x multiplier they
# don't deserve.
#
# Operationally a no-op today (LINK_EVENTS_TO_COMPANIES feature flag is
# off; no event-to-company junctions are written by the trade signal
# builder).  The fix lives behind the flag so the math is correct
# whenever the flag flips on.

# Fallback ``data_confidence`` when the field is NULL on the exposure
# row.  0.8 = "moderate confidence" — partner has surfaced this
# exposure but hasn't formally graded it.  Conservative middle ground
# between assuming high confidence (1.0) and assuming low (0.5).
_COMPANY_DATA_CONFIDENCE_DEFAULT = 0.8

# Backwards-compat alias for the pre-9.6 constant.  Now serves only as
# the ceiling for the relevance calculation: an exposure_score=1.0,
# data_confidence=1.0 row yields relevance = 1.0 directly (mapped to
# 1.30x multiplier).  The 0.85 number itself no longer appears in any
# calculation but is preserved as a constant for code archaeologists.
_COMPANY_RELEVANCE = 0.85  # pre-9.6 default; no longer used in calculation


def _compute_company_relevance(
    exposure_score: float,
    data_confidence: Optional[float],
) -> float:
    """Derive a ``RiskEventCompany.relevance_score`` from exposure data.

    Multiplicative formula: ``relevance = exposure_score × data_confidence``
    (with ``_COMPANY_DATA_CONFIDENCE_DEFAULT`` when data_confidence is
    NULL).  Result is on ``[0.0, 1.0]``.

    Why multiplicative
    ------------------
    Both inputs are on ``[0.0, 1.0]`` and represent independent
    dimensions of "how much this company should inherit risk from this
    event."  Multiplication naturally captures:

      * exposure_score=1.0, data_confidence=1.0  →  relevance=1.0  (max)
      * exposure_score=0.5, data_confidence=0.8  →  relevance=0.40 (moderate)
      * exposure_score=0.1, data_confidence=0.5  →  relevance=0.05 (very low)

    ``RiskEventCompany.relevance_score`` then maps through
    ``event_impact.relevance_score_to_multiplier`` to a ``[0.70, 1.30]``
    impact multiplier (linear interpolation, 0.5 → 1.0 neutral).

    Why not exposure_score alone
    ----------------------------
    data_confidence captures partner uncertainty in the exposure value
    itself — an "estimated" exposure of 0.7 with low data_confidence
    should not move scoring as much as a "verified" exposure of 0.5.
    Multiplying lets uncertain partner inputs flow through to scoring
    weight automatically.
    """
    confidence = (
        data_confidence
        if data_confidence is not None
        else _COMPANY_DATA_CONFIDENCE_DEFAULT
    )
    relevance = exposure_score * confidence
    return max(0.0, min(1.0, relevance))

# ---------------------------------------------------------------------------
# 9.1.A — Coverage-artifact guard (soft warning)
# ---------------------------------------------------------------------------
# TRADE_CONCENTRATION shares are computed against ``sum(country_totals)``
# for the (material, year) group — i.e. the tracked-reporter pool, NOT
# global production.  When the pool is thin (few reporters, low total $),
# top-country shares can be inflated by simple data-sparsity rather than
# real-world concentration.  These constants drive a SOFT guard: events
# still fire (so partner sees what the data shows) but get tagged with
# ``data_coverage_warning=True`` in metadata_json and a reduced
# ``confidence_score`` so downstream scoring can weight them lower.
#
# A hard guard (suppress events entirely) was rejected for Phase 1 — it
# would require a per-material "known producer coverage" lookup that
# doesn't exist yet, and risks silently dropping real signals for
# materials with narrow producer bases.

# Minimum tracked reporter count for full-confidence concentration scoring.
# Below this, the warning fires.  3 is intentionally low — it avoids
# suppressing real signals for materials with narrow producer bases.
_MIN_REPORTERS_FOR_FULL_CONFIDENCE = 3

# Minimum tracked world_total for full-confidence concentration scoring.
# Pre-9.4 this fired the coverage warning for groups in the $1M-$5M
# range.  After 9.4 ``_MIN_WORLD_TOTAL_USD`` was raised to $5M so that
# entire band is now filtered out before the warning check is reached.
# This constant is RETAINED at $5M for backwards compatibility with the
# coverage-warning derivation in the loops, but the world_total branch
# of that check is now dead code — only the reporter_count branch fires
# the warning in practice.  Kept (rather than deleted) so a future
# Phase 1.5 change that lowers ``_MIN_WORLD_TOTAL_USD`` re-enables this
# branch without recomputing the constant.
_COVERAGE_THIN_WORLD_TOTAL = _MIN_WORLD_TOTAL_USD  # $5M

# Confidence multiplier when ``data_coverage_warning`` is set.  0.6 ×
# 0.75 (the standard data-derived confidence) = 0.45 — deliberately
# under the 0.60 floor in ``compute_effective_confidence`` so thin-data
# concentration events do NOT get the high-severity confidence boost.
_COVERAGE_WARNING_CONFIDENCE_FACTOR = 0.6

# Base confidence_score for data-derived events.  Refactored to a
# module-level constant in 2026-06 (was hardcoded in two places inside
# ``_upsert_event``).
_BASE_CONFIDENCE_SCORE = 0.75

# 9.3.A (2026-06): inherent confidence ceiling for IMPORT_DECLINE events.
#
# Why lower than ``_BASE_CONFIDENCE_SCORE``: import-side declines have
# more plausible causes than export-side declines.  An export drop has
# fewer competing explanations (supply shock OR demand collapse for that
# producer's output).  An import drop can be ANY of: supply shock
# upstream, the consumer's own demand cycle, substitution to a different
# supplier, inventory drawdown, currency moves, stockpiling reversal.
# Treating import-side declines at the same confidence as export-side
# overstates how much we know about WHY the drop happened.
#
# Additionally, when a real upstream supply shock occurs, the producer-
# side EXPORT_DECLINE AND consumer-side IMPORT_DECLINE events both fire
# from the same underlying signal.  Treating both at equal confidence
# double-counts the cause.  The lower IMPORT_DECLINE confidence
# acknowledges the consumer-side echo carries less independent
# information than the producer-side observation.
#
# 0.55 is deliberately under the 0.60 floor in
# ``compute_effective_confidence`` so IMPORT_DECLINE events do NOT
# benefit from the high-severity confidence boost regardless of their
# severity score.  EXPORT_DECLINE events DO benefit from the floor
# (their base confidence is 0.75 → severity-boosted to >=0.60), so the
# practical effect is that catastrophic-severity (1.0) EXPORT_DECLINE
# events score meaningfully higher than catastrophic-severity (1.0)
# IMPORT_DECLINE events at the same drop magnitude.
_IMPORT_DECLINE_CONFIDENCE_SCORE = 0.55


# ---------------------------------------------------------------------------
# Severity helpers
# ---------------------------------------------------------------------------

def _concentration_severity(share: float) -> float:
    """Map a top-country share to severity.

    9.1.C fix (2026-06): added the ``>=0.95`` bucket at severity 1.0.
    The previous ceiling was 0.90 — investigation against
    ``event_impact.compute_event_impact`` showed severity is a direct
    multiplier into final event impact and the validation ceiling is
    1.0, so the 0.90 cap was leaving ~11% of theoretical impact on the
    table for true-monopoly events without any documented reason.

    Calibration (post-9.1.C):

        60-69%   →  0.55  (elevated — diversification advisable)
        70-79%   →  0.70  (high — single supplier dominance)
        80-89%   →  0.82  (very high — critical vulnerability)
        90-94%   →  0.90  (extreme — near-monopoly supply)
        >=95%    →  1.00  (effective monopoly)
    """
    if share >= 0.95:
        return 1.00
    if share >= 0.90:
        return 0.90
    if share >= 0.80:
        return 0.82
    if share >= 0.70:
        return 0.70
    return 0.55  # 0.60–0.69


def _herfindahl_hirschman_index(country_totals: dict[str, float]) -> float:
    """Compute HHI for a (material, year) group on the standard 0-10000 scale.

    9.1.B fix (2026-06): added so the structural concentration of the
    rest-of-market is visible alongside the top-country share.  A 60%
    top with the remaining 40% spread across many small players is a
    different risk profile than a 60% top with one 35% second player —
    raw top-share doesn't distinguish them but HHI does.

    Used only as event metadata; does NOT change the firing logic or
    severity calculation today.  Stored on ``RiskEvent.metadata_json``
    so partner can sanity-check concentration events without
    recomputing.  Phase 1.5 may migrate severity to be HHI-derived
    rather than top-share-derived; metadata trail makes that change
    backwards-compatible.

    Edge case: ``world_total == 0`` returns 0.0 (no concentration to
    measure).  Caller is expected to have already filtered groups with
    ``world_total < _MIN_TRADE_VALUE_USD``.
    """
    world_total = sum(country_totals.values())
    if world_total <= 0:
        return 0.0
    return sum((v / world_total) ** 2 for v in country_totals.values()) * 10_000.0


def _drop_severity(drop_fraction: float) -> float:
    """Map a YoY decline magnitude to severity.

    ``drop_fraction`` is the magnitude of decline (e.g. 0.35 for a 35% drop).

    9.2.C fix (2026-06): added a ``>=0.60`` bucket at severity 1.0.  Same
    reasoning as 9.1.C — severity is a direct linear multiplier in
    ``event_impact.compute_event_impact`` and the validation ceiling is
    1.0, so the previous 0.88 cap was leaving ~12% of theoretical impact
    on the table for catastrophic-decline events (think Indonesia 2014
    nickel ore export ban, ~70% drop).  The 0.50-0.59 bucket holds at
    0.88 since that's still "severe but not catastrophic" territory.

    9.2.B (2026-06): the first bucket's range is now [0.25, 0.30) after
    bumping ``_DROP_THRESHOLD`` from 0.20 to 0.25.  The severity value
    (0.50) is unchanged — only the range moves.

    Calibration (post-9.2.B + 9.2.C):

        25-29%   →  0.50  (moderate disruption signal)
        30-39%   →  0.65  (significant — possible restriction)
        40-49%   →  0.78  (severe)
        50-59%   →  0.88  (critical)
        >=60%    →  1.00  (catastrophic — likely policy ban or
                            production collapse)
    """
    if drop_fraction >= 0.60:
        return 1.00
    if drop_fraction >= 0.50:
        return 0.88
    if drop_fraction >= 0.40:
        return 0.78
    if drop_fraction >= 0.30:
        return 0.65
    return 0.50  # 0.25–0.29


def _stable_event_key(
    event_subtype: str,
    material_id: int,
    reporter_country: str,
    year: int,
) -> str:
    """Return the stable identifier for a derived trade signal.

    TRADE_CONCENTRATION / EXPORT_RESTRICTION / IMPORT_DISRUPTION events
    derived from trade_flows are conceptually *states*, not point-in-time
    occurrences.  "Country X is 67% of material Y's exports for year Z" is
    a state that updates as the underlying calculation changes (e.g. when
    confidence weighting in ``_get_annual_totals`` shifts the country shares).

    Hashing on this stable identifier — rather than on the title (which
    encodes the mutable percentage) — means re-running this module after a
    confidence calibration change UPDATEs the existing row in place rather
    than creating a duplicate alongside it.

    The four-tuple ``(event_subtype, material_id, reporter_country, year)``
    uniquely identifies one signal-state row.  Same identity → same hash →
    UPSERT.
    """
    return f"{event_subtype}|material_id={material_id}|country={reporter_country}|year={year}"


def _content_hash(stable_key: str) -> str:
    """SHA-256 of the parameter-stable event identifier."""
    return hashlib.sha256(stable_key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _upsert_event(
    db: Session,
    title: str,
    summary: str,
    event_date: datetime,
    severity: float,
    event_subtype: str,
    reporter_country: str,
    material_id: int,
    material_name: str,
    year: int,
    confidence_score: float = _BASE_CONFIDENCE_SCORE,
    extra_metadata: Optional[dict] = None,
) -> tuple[int, bool]:
    """Insert or update a RiskEvent row keyed on the parameter-stable hash.

    Returns ``(event_id, was_inserted)``:
      * ``was_inserted=True``  — new row created.  Caller should write
        material / HS / company junction rows for the first time.
      * ``was_inserted=False`` — existing row updated in place.  Caller
        should refresh material + HS junctions (delete + re-insert) but
        leave company junctions alone (partial-rewrite semantics per
        2026-05-11 audit decision; see docs/scoring-audit-2026-05-addendum.md).

    The stable hash means second runs against unchanged data hit the
    existing row; the UPDATE refreshes ``updated_at`` (via the ORM's
    ``onupdate=func.now()``) but every other column is rewritten with the
    current calculation's output, so the row reflects the latest state.

    Args:
        confidence_score: Defaults to ``_BASE_CONFIDENCE_SCORE`` (0.75).
            Callers can pass a reduced value when a soft data-quality
            guard fires — see 9.1.A coverage-warning logic in
            ``build_trade_signals`` for the canonical example.
        extra_metadata: Optional dict merged into ``metadata_json``
            after the standard keys.  Used by the concentration signal
            to attach ``hhi`` + ``data_coverage_warning`` +
            ``reporter_count_in_group`` + ``world_total_usd`` so the
            structural-concentration sanity-check + coverage guard are
            visible without recomputation (9.1.A + 9.1.B).
    """
    ch = _content_hash(_stable_event_key(
        event_subtype, material_id, reporter_country, year,
    ))

    metadata = {
        # event_subtype kept in metadata_json for one release cycle —
        # downstream readers migrated to the typed column 2026-05-05;
        # JSON copy retained for backwards compatibility while we
        # confirm nothing else reads it.  Drop after one production cycle.
        "event_subtype": event_subtype,
        "reporter_country": reporter_country,
        "material_id": material_id,
        "material_name": material_name,
        "reference_year": year,
        "source": "trade_flow_analysis",
    }
    if extra_metadata:
        metadata.update(extra_metadata)

    existing = db.scalar(
        select(RiskEvent).where(RiskEvent.content_hash == ch).limit(1)
    )
    if existing is not None:
        # In-place UPDATE.  Rewrite every column that depends on the
        # current calculation so the row reflects the latest state.
        existing.event_subtype = event_subtype
        existing.event_date = event_date
        existing.title = title
        existing.summary = summary
        existing.severity_score = round(severity, 2)
        existing.confidence_score = round(confidence_score, 2)
        existing.risk_categories_json = [RiskCategory.GEOPOLITICAL_TRADE.value]
        existing.geography_json = {"primary": reporter_country}
        existing.metadata_json = metadata
        db.flush()
        return existing.id, False

    event = RiskEvent(
        event_type="GEOPOLITICAL_TRADE",
        event_subtype=event_subtype,  # typed col (migration 040)
        event_date=event_date,
        title=title,
        summary=summary,
        severity_score=round(severity, 2),
        confidence_score=round(confidence_score, 2),
        risk_categories_json=[RiskCategory.GEOPOLITICAL_TRADE.value],
        geography_json={"primary": reporter_country},
        content_hash=ch,
        metadata_json=metadata,
    )
    db.add(event)
    db.flush()
    return event.id, True


def _clear_material_and_hs_junctions(db: Session, event_id: int) -> None:
    """Delete RiskEventMaterial + RiskEventHsMapping rows for an event.

    Called on UPDATE path of ``_upsert_event`` so the subsequent
    ``_link_material`` call writes fresh junctions without hitting
    ``uq_risk_event_material`` / ``uq_risk_event_hs_mapping`` constraints.

    9.8 history note (2026-06): Pre-9.6 this function explicitly
    excluded company junctions per the 2026-05-11 partial-rewrite
    audit decision — the rationale was "company exposure changes
    evolve more slowly than trade-share calculations and re-querying
    CompanyMaterialExposure on every UPSERT pass would add noise
    without changing scoring."  After 9.6 made ``relevance_score``
    derive from ``exposure_score × data_confidence`` (both queryable
    fields on CompanyMaterialExposure), that rationale no longer
    holds — partner updates to either field will silently NOT
    propagate to existing junctions unless we refresh them.  See
    :py:func:`_clear_company_junctions` for the refresh path; it is
    called alongside this function on UPDATE.
    """
    from sqlalchemy import delete
    db.execute(
        delete(RiskEventMaterial).where(
            RiskEventMaterial.risk_event_id == event_id,
        )
    )
    db.execute(
        delete(RiskEventHsMapping).where(
            RiskEventHsMapping.risk_event_id == event_id,
        )
    )


def _clear_company_junctions(db: Session, event_id: int) -> None:
    """Delete RiskEventCompany rows for an event.

    9.8 fix (2026-06): added so the UPSERT UPDATE path refreshes
    company junctions in addition to material + HS junctions.  Pairs
    with the 9.6 change that made ``relevance_score`` derive from
    queryable exposure data — without this companion, partner updates
    to ``CompanyMaterialExposure.exposure_score`` or
    ``data_confidence`` would silently not propagate to existing
    junctions.

    Operationally a no-op today because ``LINK_EVENTS_TO_COMPANIES``
    is off; no RiskEventCompany rows exist to clear.  When the flag
    flips on this becomes load-bearing — without it, the first
    UPSERT-after-flag-flip would dual-write company junctions
    (existing partial-rewrite junctions kept + new junctions
    inserted) and violate the implied 1-link-per-(event, company)
    invariant.
    """
    from sqlalchemy import delete
    db.execute(
        delete(RiskEventCompany).where(
            RiskEventCompany.risk_event_id == event_id,
        )
    )


def _link_material(
    db: Session,
    event_id: int,
    material_id: int,
    hs_mapping_ids: list[int] | None = None,
) -> None:
    """Write RiskEventMaterial and (when hs_mapping_ids is set) RiskEventHsMapping rows."""
    # relevance_score=1.0 for both junction types: these are direct
    # attributions — the trade flow's HS code maps to this material
    # (so the material relevance IS 1.0 by definition), and that same
    # HS code is the trade_flow_match (no inference).  Compared to
    # RiskEventCompany junctions where relevance is derived from
    # exposure_score × data_confidence (9.6), material/HS junctions
    # don't have a "how exposed" or "how confident" axis — the trade
    # flow either IS this material's flow or it isn't.
    db.add(RiskEventMaterial(
        risk_event_id=event_id,
        material_id=material_id,
        relevance_score=1.0,
        match_reason=_MATCH_REASON_MATERIAL,
    ))
    for hs_id in (hs_mapping_ids or []):
        db.add(RiskEventHsMapping(
            risk_event_id=event_id,
            hs_mapping_id=hs_id,
            relevance_score=1.0,
            match_reason=_MATCH_REASON_HS_MAPPING,
        ))


def _link_companies(
    db: Session,
    event_id: int,
    material_id: int,
    reporter_country: str,
) -> int:
    """Link all companies sourcing material_id from reporter_country.

    Returns the number of links written. Foundation phase 3 disables ingestion-
    time event → company linking via ``feature_flags.LINK_EVENTS_TO_COMPANIES``;
    when False (the default), this function counts the matches that *would*
    have been written, emits one ``structlog`` WARNING per call, and returns 0
    so the upstream ``company_links`` counter stays honest.

    9.6 changes (2026-06):
      * Relevance score is now computed per-company via
        ``_compute_company_relevance(exposure_score, data_confidence)`` —
        not a hardcoded 0.85.  Companies with low exposure or low-
        confidence data get proportionally lower relevance.
      * Multiple exposures for the same (company, material, country)
        across different supply_chain_stages are deduped by picking the
        HIGHEST exposure_score row as the representative.  Previously
        the loop arbitrarily kept whichever exposure SQL returned first.
      * Rows with ``exposure_score == 0`` are skipped — a company with
        zero exposure to a material shouldn't inherit risk from a
        material-anchored event.  Previously they'd have received a
        ``_COMPANY_RELEVANCE`` (0.85) link anyway.
    """
    rows = db.scalars(
        select(CompanyMaterialExposure).where(
            CompanyMaterialExposure.material_id == material_id,
            CompanyMaterialExposure.source_geography == reporter_country,
        )
    ).all()

    if not feature_flags.LINK_EVENTS_TO_COMPANIES:
        suppressed = len({exposure.company_id for exposure in rows})
        if suppressed:
            log.warning(
                "trade_signal_builder.link_companies.suppressed",
                event_id=event_id,
                material_id=material_id,
                reporter_country=reporter_country,
                suppressed_company_count=suppressed,
                reason="LINK_EVENTS_TO_COMPANIES feature flag is disabled",
            )
        return 0

    # 9.6: pick the highest-exposure row per company as the representative
    # (instead of "first row SQL returns").  A company can have separate
    # exposure rows per supply_chain_stage (mining / refining / cell / pack
    # / oem) for the same (material, country).  Using the highest exposure
    # captures the strongest link without writing N junction rows for the
    # same company-event pair.
    best_per_company: dict = {}
    for exposure in rows:
        if exposure.exposure_score is None or exposure.exposure_score <= 0:
            continue
        cid = exposure.company_id
        existing = best_per_company.get(cid)
        if existing is None or exposure.exposure_score > existing.exposure_score:
            best_per_company[cid] = exposure

    linked = 0
    for cid, exposure in best_per_company.items():
        relevance = _compute_company_relevance(
            exposure.exposure_score, exposure.data_confidence,
        )
        db.add(RiskEventCompany(
            id=uuid.uuid4(),
            risk_event_id=event_id,
            company_id=cid,
            relevance_score=relevance,
            match_reason=_MATCH_REASON_COMPANY,
        ))
        linked += 1

    return linked


# ---------------------------------------------------------------------------
# Signal derivation
# ---------------------------------------------------------------------------

def _get_annual_totals(
    db: Session,
    import_export_flag: str = "export",
) -> dict[tuple[int, str, int], float]:
    """
    Query trade_flows for WLD partner and sum by (material_id, reporter_country, year).

    Args:
        import_export_flag: ``"export"`` (default) or ``"import"``.

    Returns {(material_id, reporter_country, year): total_trade_value_usd}.
    Only includes rows with partner_country='WLD' and material_id IS NOT NULL.

    9.5 (2026-06): confidence weighting REMOVED.
    ----------------------------------------------------------------------------
    A 2026-05-09 audit extension had multiplied each row's
    ``trade_value_usd`` by ``HsCodeMaterialMapping.confidence`` before
    summing, with the intent of down-weighting ambiguous prefix matches
    (e.g. 4-digit "ores and concentrates" catch-alls).  The 2026-06 Trade
    Volumes audit (Section 9.5) reverted that change because the formula
    didn't match the data semantics.

    The semantic mismatch
    ~~~~~~~~~~~~~~~~~~~~~
    ``seed_hs_mappings.py`` documents (line 11-13) that confidence is a
    PREFIX SPECIFICITY score, NOT a fractional allocation.  A 0.6
    confidence for "HS 2615 → Vanadium" means "we are 60% sure this code
    refers to vanadium" — independently of the same code's mappings to
    Niobium, Tantalum, Zirconium, each ALSO at confidence 0.6.

    The previous ``trade_value × confidence`` formula treated the 0.6 as
    a fractional weight.  For HS 2615 at confidence 0.6 across four
    materials, a $100M trade flow was attributed as $60M × 4 = $240M of
    cross-material attribution from $100M of actual trade.  Each per-
    material analysis was internally consistent (same factor on
    numerator and denominator of the share calculation), but:

      * Raw $ amounts in event metadata were specificity-weighted, not
        actual trade — partner reading "USD 60M of vanadium exports"
        would not realize the actual trade was $100M of an ambiguous
        prefix that also got attributed to three other materials.
      * Cross-year drift after 2026-06 ``breakdownMode='plus'``: pre-plus
        data is 4-digit (lower confidence, e.g. 0.5-0.7); post-plus is
        6-digit (higher confidence, e.g. 0.8-1.0).  A 2024→2025 drop
        comparison would mix lower-weighted prior-year totals with
        higher-weighted current-year totals, manufacturing artificial
        "growth" or "decline" signals that aren't real.
      * Cross-country distortion when countries trade in different HS
        codes: a country's share of "high-confidence" prefixes gets
        boosted relative to a country in "low-confidence" prefixes,
        even when both are tracking the same material.

    Post-revert behavior
    ~~~~~~~~~~~~~~~~~~~~
    Each row's raw ``trade_value_usd`` is summed directly.  Resulting
    metadata $ amounts match actual reported trade.  Concentration
    shares, drop fractions, and HHI all reflect real flows.  The
    intuition that "ambiguous prefix matters less" is lost at this
    layer — handled instead via the per-row ``hs_mapping_confidence``
    that's still preserved in ``TradeFlow.metadata_json`` (9.1.B / 9.2
    extra_metadata), available for ad-hoc partner sanity-checks or a
    future Phase 1.5 fix that normalises confidence per HS code (so
    confidences for HS 2615 across Nb/Ta/Va/Zr sum to 1.0 and the
    formula becomes a proper fractional allocation).
    """
    rows = db.execute(
        select(
            TradeFlow.material_id,
            TradeFlow.reporter_country,
            TradeFlow.period,
            func.sum(TradeFlow.trade_value_usd).label("total_usd"),
        )
        .where(
            TradeFlow.partner_country == "WLD",
            TradeFlow.import_export_flag == import_export_flag,
            TradeFlow.material_id.is_not(None),
            TradeFlow.trade_value_usd.is_not(None),
        )
        .group_by(TradeFlow.material_id, TradeFlow.reporter_country, TradeFlow.period)
    ).all()

    result: dict[tuple[int, str, int], float] = {}
    for material_id, country, period_str, total in rows:
        try:
            year = int(period_str)
        except (TypeError, ValueError):
            continue
        if material_id is not None and total and total > 0:
            result[(material_id, country, year)] = float(total)

    return result


def _get_material_names(db: Session) -> dict[int, str]:
    from app.models.supply import Material
    rows = db.execute(select(Material.id, Material.canonical_name)).all()
    return {r.id: r.canonical_name for r in rows}


def _get_hs_mapping_ids_by_signal(
    db: Session,
    import_export_flag: str = "export",
) -> dict[tuple[int, str, int], list[int]]:
    """
    Return all hs_mapping_ids that contributed to each (material_id, country,
    year) trade signal group.

    Queried once upfront and passed into the main signal loop so each event
    can populate risk_event_hs_mappings at creation time without a per-event
    DB round-trip.

    Returns {(material_id, reporter_country, year): [hs_mapping_id, ...]}.
    Only includes flows with hs_mapping_id IS NOT NULL (i.e. rows ingested
    after Phase 1.5 or backfilled via the migration 027 backfill query).
    Historical rows without hs_mapping_id are silently omitted — they still
    write risk_event_materials via _link_material but contribute no stage
    attribution.
    """
    rows = db.execute(
        select(
            TradeFlow.material_id,
            TradeFlow.reporter_country,
            TradeFlow.period,
            TradeFlow.hs_mapping_id,
        )
        .where(
            TradeFlow.partner_country == "WLD",
            TradeFlow.import_export_flag == import_export_flag,
            TradeFlow.material_id.is_not(None),
            TradeFlow.hs_mapping_id.is_not(None),
        )
        .distinct()
    ).all()

    result: dict[tuple[int, str, int], list[int]] = {}
    for mat_id, country, period_str, hs_id in rows:
        try:
            year = int(period_str)
        except (TypeError, ValueError):
            continue
        key = (mat_id, country, year)
        result.setdefault(key, []).append(hs_id)
    return result


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def build_trade_signals(
    db: Session,
    years: Optional[list[int]] = None,
) -> dict[str, int]:
    """
    Derive GEOPOLITICAL_TRADE risk events from trade_flows and persist them.

    Processes both export flows (from ingest-comtrade --flow-code X) and import
    flows (from ingest-comtrade --flow-code M). Safe to run when only one set is
    present — missing flows produce zero events for that signal type.

    Args:
        db:    SQLAlchemy session. Commits internally after all events are created.
        years: Restrict analysis to these years. Default: all years in trade_flows.

    Returns:
        {
            "concentration_events": int,  # TRADE_CONCENTRATION (export share ≥60%)
            "export_drop_events": int,    # EXPORT_DROP (producer exports fell ≥20% YoY)
            "import_drop_events": int,    # IMPORT_DROP (consumer imports fell ≥20% YoY)
            "company_links": int,         # risk_event_companies rows created
            "skipped_existing": int,      # events skipped (content_hash exists)
        }
    """
    log.info("trade_signal_builder.start")

    annual_totals = _get_annual_totals(db, import_export_flag="export")
    annual_import_totals = _get_annual_totals(db, import_export_flag="import")

    # Stage attribution: (material_id, country, year) → [hs_mapping_id, ...]
    # Populated for trade flows ingested after Phase 1.5 (hs_mapping_id column).
    # Empty for historical rows — those events still write risk_event_materials.
    hs_ids_by_export_signal = _get_hs_mapping_ids_by_signal(db, import_export_flag="export")
    hs_ids_by_import_signal = _get_hs_mapping_ids_by_signal(db, import_export_flag="import")

    if not annual_totals and not annual_import_totals:
        log.warning("trade_signal_builder.no_trade_flows")
        return {"concentration_events": 0, "export_drop_events": 0,
                "import_drop_events": 0, "company_links": 0, "skipped_existing": 0}

    material_names = _get_material_names(db)

    # Determine available years across both export and import flows.
    all_export_years = {year for (_, _, year) in annual_totals.keys()}
    all_import_years = {year for (_, _, year) in annual_import_totals.keys()}
    all_years = sorted(all_export_years | all_import_years)
    if years:
        all_years = [y for y in all_years if y in years]

    # Group export flows: (material_id, year) → {country: total_usd}
    by_mat_year: dict[tuple[int, int], dict[str, float]] = {}
    for (mat_id, country, year), total in annual_totals.items():
        if year not in all_years:
            continue
        by_mat_year.setdefault((mat_id, year), {})[country] = total

    # Group import flows: (material_id, year) → {country: total_usd}
    by_mat_year_imports: dict[tuple[int, int], dict[str, float]] = {}
    for (mat_id, country, year), total in annual_import_totals.items():
        if year not in all_years:
            continue
        by_mat_year_imports.setdefault((mat_id, year), {})[country] = total

    concentration_events = 0
    concentration_events_updated = 0
    export_drop_events = 0
    export_drop_events_updated = 0
    import_drop_events = 0
    import_drop_events_updated = 0
    company_links = 0
    # Retained for backwards-compat with callers reading the result dict;
    # under parameter-stable UPSERT semantics this counter always stays 0
    # since same-data re-runs UPDATE in place rather than skip.
    skipped_existing = 0  # noqa: F841 — used in result dict

    for (mat_id, year), country_totals in sorted(by_mat_year.items()):
        mat_name = material_names.get(mat_id, f"material_{mat_id}")
        world_total = sum(country_totals.values())

        # 9.4 (2026-06): per-group hard floor uses ``_MIN_WORLD_TOTAL_USD``
        # (bumped from $1M to $5M to align with the coverage-warning
        # band).  Per-country flow checks below still use the per-country
        # constant (``_MIN_PER_COUNTRY_TRADE_USD`` aka the legacy
        # ``_MIN_TRADE_VALUE_USD`` alias).
        if world_total < _MIN_WORLD_TOTAL_USD:
            continue

        # 9.1.A + 9.1.B (2026-06): compute reporter pool size, HHI, and
        # coverage-warning flag once per (material, year) so every
        # event for this group inherits the same group-level metadata.
        reporter_count = len(country_totals)
        hhi = _herfindahl_hirschman_index(country_totals)
        data_coverage_warning = (
            reporter_count < _MIN_REPORTERS_FOR_FULL_CONFIDENCE
            or world_total < _COVERAGE_THIN_WORLD_TOTAL
        )
        coverage_confidence = (
            _BASE_CONFIDENCE_SCORE * _COVERAGE_WARNING_CONFIDENCE_FACTOR
            if data_coverage_warning
            else _BASE_CONFIDENCE_SCORE
        )
        if data_coverage_warning:
            log.info(
                "trade_signal_builder.concentration_coverage_warning",
                material=mat_name,
                year=year,
                reporter_count=reporter_count,
                world_total_usd=world_total,
                reason=(
                    "low_reporter_count"
                    if reporter_count < _MIN_REPORTERS_FOR_FULL_CONFIDENCE
                    else "thin_world_total"
                ),
            )

        # ── TRADE_CONCENTRATION signals ──────────────────────────────────
        for country, country_total in country_totals.items():
            share = country_total / world_total
            if share < _CONCENTRATION_THRESHOLD:
                continue

            severity = _concentration_severity(share)
            title = (
                f"{country} accounts for {share:.0%} of tracked "
                f"{mat_name} exports ({year})"
            )
            summary = (
                f"Trade flow analysis shows {country} controlling {share:.1%} "
                f"of total tracked reporter exports for {mat_name} in {year} "
                f"(USD {country_total:,.0f} of USD {world_total:,.0f} total). "
                f"High geographic concentration creates single-country supply dependency."
            )
            event_date = datetime(year, 12, 31, tzinfo=timezone.utc)

            event_id, was_inserted = _upsert_event(
                db,
                title=title,
                summary=summary,
                event_date=event_date,
                severity=severity,
                event_subtype="TRADE_CONCENTRATION",
                reporter_country=country,
                material_id=mat_id,
                material_name=mat_name,
                year=year,
                # 9.1.A: reduced confidence when coverage is thin so
                # downstream scoring doesn't trust the share number.
                confidence_score=coverage_confidence,
                # 9.1.B: HHI + coverage signals visible in metadata for
                # partner sanity-check without recomputation.
                extra_metadata={
                    "share": round(share, 4),
                    "hhi": round(hhi, 1),
                    "reporter_count_in_group": reporter_count,
                    "world_total_usd": round(world_total, 2),
                    "data_coverage_warning": data_coverage_warning,
                },
            )
            if was_inserted:
                _link_material(
                    db, event_id, mat_id,
                    hs_mapping_ids=hs_ids_by_export_signal.get((mat_id, country, year)),
                )
                linked = _link_companies(db, event_id, mat_id, country)
                company_links += linked
                concentration_events += 1
            else:
                # Existing row updated in place — refresh material +
                # HS + company junctions.  9.8 reversed the 2026-05-11
                # decision to leave company junctions alone: 9.6 made
                # relevance derive from queryable exposure data, so
                # partner updates to exposure_score/data_confidence
                # would not propagate without the company-junction
                # refresh.
                _clear_material_and_hs_junctions(db, event_id)
                _clear_company_junctions(db, event_id)
                _link_material(
                    db, event_id, mat_id,
                    hs_mapping_ids=hs_ids_by_export_signal.get((mat_id, country, year)),
                )
                linked = _link_companies(db, event_id, mat_id, country)
                company_links += linked
                concentration_events_updated += 1

            log.info(
                "trade_signal_builder.concentration_event",
                country=country,
                material=mat_name,
                year=year,
                share=round(share, 3),
                severity=severity,
                companies_linked=linked,
                was_inserted=was_inserted,
            )

        # ── EXPORT_DECLINE signals ────────────────────────────────────────
        # 9.2.E rename (2026-06): subtype changed from EXPORT_RESTRICTION
        # to EXPORT_DECLINE to reflect what we actually measured (a
        # YoY decline) rather than what we inferred (a policy
        # restriction).  Direct policy-driven sources — gta.py,
        # opensanctions.py, ingest_federal_register.py — continue to
        # emit EXPORT_RESTRICTION for genuinely-observed restrictions.
        # evidence_aggregator.py was updated in the same commit to
        # match both subtypes for the export_restriction_exposure
        # sub-input so scoring routing is preserved.
        prev_year = year - 1
        prev_totals = by_mat_year.get((mat_id, prev_year), {})
        if not prev_totals:
            continue

        # Coverage warning derived from the PRIOR-year group, since that
        # is the denominator of the drop calculation.  If prev_year has
        # thin coverage the entire (material, prev_year) group is shaky
        # and EVERY drop calculation against it should be flagged.
        prev_reporter_count = len(prev_totals)
        prev_world_total = sum(prev_totals.values())
        prev_year_data_coverage_warning = (
            prev_reporter_count < _MIN_REPORTERS_FOR_FULL_CONFIDENCE
            or prev_world_total < _COVERAGE_THIN_WORLD_TOTAL
        )

        for country, current_total in country_totals.items():
            prev_total = prev_totals.get(country)
            if prev_total is None or prev_total < _MIN_TRADE_VALUE_USD:
                continue

            # 9.2.A fix (2026-06): symmetric min-trade-value gate on the
            # current year.  Without this, partial-backfill coverage for
            # the current year (e.g. 2024 only partly filled when we hit
            # 2024-vs-2023) produces false "near-100%" decline signals
            # at severity 1.0 — those rows look catastrophic but are
            # really data-coverage gaps, not real export drops.
            if current_total < _MIN_TRADE_VALUE_USD:
                log.info(
                    "trade_signal_builder.export_decline.suppressed_thin_current",
                    country=country,
                    material=mat_name,
                    year=year,
                    prev_total_usd=prev_total,
                    current_total_usd=current_total,
                    hint=(
                        "Current-year flow is below the noise floor — "
                        "likely a coverage gap rather than a real decline. "
                        "Suppressing to avoid false catastrophic signals."
                    ),
                )
                continue

            drop_fraction = (prev_total - current_total) / prev_total
            if drop_fraction < _DROP_THRESHOLD:
                continue

            severity = _drop_severity(drop_fraction)
            # 9.2.D (2026-06): coverage-warning consistency with 9.1.A.
            # Reduce confidence when either year's denominator is thin.
            drop_coverage_confidence = (
                _BASE_CONFIDENCE_SCORE * _COVERAGE_WARNING_CONFIDENCE_FACTOR
                if prev_year_data_coverage_warning
                else _BASE_CONFIDENCE_SCORE
            )
            title = (
                f"{country} {mat_name} exports fell {drop_fraction:.0%} "
                f"YoY ({prev_year}→{year})"
            )
            summary = (
                f"{country} recorded a {drop_fraction:.1%} year-on-year decline in "
                f"{mat_name} exports ({year} vs {prev_year}): "
                f"USD {current_total:,.0f} vs USD {prev_total:,.0f}. "
                f"Significant export declines may signal emerging restrictions, "
                f"production disruptions, demand collapse, or other factors — "
                f"this signal infers a decline from trade volumes and does not "
                f"directly observe the cause."
            )
            event_date = datetime(year, 12, 31, tzinfo=timezone.utc)

            event_id, was_inserted = _upsert_event(
                db,
                title=title,
                summary=summary,
                event_date=event_date,
                severity=severity,
                # 9.2.E: EXPORT_DECLINE replaces EXPORT_RESTRICTION for
                # trade-volume-derived inferred declines.  Direct policy
                # sources continue to emit EXPORT_RESTRICTION.
                event_subtype="EXPORT_DECLINE",
                reporter_country=country,
                material_id=mat_id,
                material_name=mat_name,
                year=year,
                confidence_score=drop_coverage_confidence,
                extra_metadata={
                    "drop_fraction": round(drop_fraction, 4),
                    "current_total_usd": round(current_total, 2),
                    "prev_total_usd": round(prev_total, 2),
                    "prev_year_reporter_count": prev_reporter_count,
                    "prev_year_world_total_usd": round(prev_world_total, 2),
                    "data_coverage_warning": prev_year_data_coverage_warning,
                    "inference_caveat": (
                        "Subtype EXPORT_DECLINE inferred from a volume "
                        "drop; cause not directly observed. Distinct "
                        "from EXPORT_RESTRICTION events emitted by "
                        "policy-driven sources (gta, opensanctions, "
                        "federal_register)."
                    ),
                    "scoring_routing_note": (
                        "EXPORT_DECLINE does NOT feed "
                        "export_restriction_exposure (Geopolitical) — "
                        "that's policy-only.  It DOES feed "
                        "trade_volatility (Material Concentration) "
                        "and country_concentration (Geopolitical) via "
                        "the broad GEOPOLITICAL_TRADE category tag, "
                        "weighted by the event's confidence_score."
                    ),
                },
            )
            if was_inserted:
                _link_material(
                    db, event_id, mat_id,
                    hs_mapping_ids=hs_ids_by_export_signal.get((mat_id, country, year)),
                )
                linked = _link_companies(db, event_id, mat_id, country)
                company_links += linked
                export_drop_events += 1
            else:
                # 9.8: UPDATE path now also refreshes company junctions
                # (see _clear_material_and_hs_junctions docstring for
                # the reversal of the 2026-05-11 partial-rewrite
                # decision).
                _clear_material_and_hs_junctions(db, event_id)
                _clear_company_junctions(db, event_id)
                _link_material(
                    db, event_id, mat_id,
                    hs_mapping_ids=hs_ids_by_export_signal.get((mat_id, country, year)),
                )
                linked = _link_companies(db, event_id, mat_id, country)
                company_links += linked
                export_drop_events_updated += 1

            log.info(
                "trade_signal_builder.export_decline_event",
                country=country,
                material=mat_name,
                year=year,
                drop_fraction=round(drop_fraction, 3),
                severity=severity,
                companies_linked=linked,
                was_inserted=was_inserted,
                data_coverage_warning=prev_year_data_coverage_warning,
            )

    # ── IMPORT_DECLINE signals ────────────────────────────────────────────────
    # 9.3.B rename (2026-06): subtype changed from IMPORT_DISRUPTION to
    # IMPORT_DECLINE.  Reasoning: ``gta.py`` ALSO emits IMPORT_DISRUPTION
    # for directly-observed import tariff/quota/ban policies; that subtype
    # is wired into ``hs_node_scorer._TARIFF_SUBTYPES`` so it correctly
    # drives the ``tariff_exposure`` sub-input.  Our volume-drop-inferred
    # signals are NOT tariff observations — they could be many things
    # (demand collapse, substitution, inventory drawdown, currency moves)
    # — and contaminating tariff_exposure with that noise is a
    # pre-existing scoring bug.  Renaming our variant to IMPORT_DECLINE
    # AND leaving _TARIFF_SUBTYPES alone (no IMPORT_DECLINE entry) fixes
    # the bug.
    #
    # 9.3.E scoring routing decision: IMPORT_DECLINE feeds NO scoring
    # pillar today.  It is informational metadata only — partner UI can
    # show it, but it does NOT contribute to material_geography_risk_scores
    # or any pillar sub-input.  Future work could design a dedicated
    # "consumer supply stress" sub-input if needed; tracked as a Phase
    # 1.5 candidate.
    #
    # 9.3.A confidence: IMPORT_DECLINE uses
    # ``_IMPORT_DECLINE_CONFIDENCE_SCORE`` (0.55) rather than the
    # default 0.75.  See that constant's docstring for the reasoning
    # (more plausible causes per drop + downstream echo problem).
    for (mat_id, year), country_totals in sorted(by_mat_year_imports.items()):
        mat_name = material_names.get(mat_id, f"material_{mat_id}")
        prev_year = year - 1
        prev_totals = by_mat_year_imports.get((mat_id, prev_year), {})
        if not prev_totals:
            continue

        # 9.2.D mirror: coverage warning derived from prior-year imports.
        prev_reporter_count = len(prev_totals)
        prev_world_total = sum(prev_totals.values())
        prev_year_data_coverage_warning = (
            prev_reporter_count < _MIN_REPORTERS_FOR_FULL_CONFIDENCE
            or prev_world_total < _COVERAGE_THIN_WORLD_TOTAL
        )

        for country, current_total in country_totals.items():
            prev_total = prev_totals.get(country)
            if prev_total is None or prev_total < _MIN_TRADE_VALUE_USD:
                continue

            # 9.2.A fix mirror: symmetric current-year noise-floor gate.
            if current_total < _MIN_TRADE_VALUE_USD:
                log.info(
                    "trade_signal_builder.import_decline.suppressed_thin_current",
                    country=country,
                    material=mat_name,
                    year=year,
                    prev_total_usd=prev_total,
                    current_total_usd=current_total,
                    hint=(
                        "Current-year flow is below the noise floor — "
                        "likely a coverage gap rather than a real drop. "
                        "Suppressing to avoid false catastrophic signals."
                    ),
                )
                continue

            drop_fraction = (prev_total - current_total) / prev_total
            if drop_fraction < _DROP_THRESHOLD:
                continue

            severity = _drop_severity(drop_fraction)
            # 9.3.A: IMPORT_DECLINE base confidence is lower than
            # EXPORT_DECLINE.  When coverage warning AND inherent-low
            # both apply, take the minimum (coverage warning is itself
            # a confidence-flooring signal at 0.45, deliberately below
            # the 0.60 high-severity boost threshold).
            inherent_import_confidence = _IMPORT_DECLINE_CONFIDENCE_SCORE
            if prev_year_data_coverage_warning:
                coverage_floor = (
                    _BASE_CONFIDENCE_SCORE * _COVERAGE_WARNING_CONFIDENCE_FACTOR
                )
                drop_coverage_confidence = min(
                    inherent_import_confidence, coverage_floor
                )
            else:
                drop_coverage_confidence = inherent_import_confidence
            title = (
                f"{country} {mat_name} imports fell {drop_fraction:.0%} "
                f"YoY ({prev_year}→{year})"
            )
            summary = (
                f"{country} recorded a {drop_fraction:.1%} year-on-year decline in "
                f"{mat_name} imports ({year} vs {prev_year}): "
                f"USD {current_total:,.0f} vs USD {prev_total:,.0f}. "
                f"Import-side declines have multiple plausible causes — "
                f"upstream supply constraint, the consumer's own demand "
                f"cycle, substitution to a different supplier, inventory "
                f"drawdown, or currency moves.  Inferred from trade volumes "
                f"(distinct from policy-driven IMPORT_DISRUPTION events "
                f"emitted by gta.py); contributes to trade_volatility and "
                f"country_concentration scoring at reduced confidence (0.55) "
                f"but does not drive tariff_exposure."
            )
            event_date = datetime(year, 12, 31, tzinfo=timezone.utc)

            event_id, was_inserted = _upsert_event(
                db,
                title=title,
                summary=summary,
                event_date=event_date,
                severity=severity,
                # 9.3.B: IMPORT_DECLINE replaces IMPORT_DISRUPTION for
                # trade-volume-inferred declines.  GTA's policy-driven
                # variant continues to emit IMPORT_DISRUPTION (and feeds
                # tariff_exposure via hs_node_scorer._TARIFF_SUBTYPES).
                event_subtype="IMPORT_DECLINE",
                reporter_country=country,
                material_id=mat_id,
                material_name=mat_name,
                year=year,
                confidence_score=drop_coverage_confidence,
                extra_metadata={
                    "drop_fraction": round(drop_fraction, 4),
                    "current_total_usd": round(current_total, 2),
                    "prev_total_usd": round(prev_total, 2),
                    "prev_year_reporter_count": prev_reporter_count,
                    "prev_year_world_total_usd": round(prev_world_total, 2),
                    "data_coverage_warning": prev_year_data_coverage_warning,
                    "scoring_routing_note": (
                        "IMPORT_DECLINE does NOT feed tariff_exposure "
                        "(that's policy-only from GTA's IMPORT_DISRUPTION "
                        "events).  It DOES feed trade_volatility "
                        "(Material Concentration) and country_concentration "
                        "(Geopolitical) via the broad GEOPOLITICAL_TRADE "
                        "category tag, weighted by the lower "
                        "confidence_score (0.55 vs EXPORT_DECLINE's 0.75)."
                    ),
                },
            )
            if was_inserted:
                _link_material(
                    db, event_id, mat_id,
                    hs_mapping_ids=hs_ids_by_import_signal.get((mat_id, country, year)),
                )
                # For import signals, the reporter is the consuming country —
                # link companies that source this material from any geography
                # (not just the consuming country itself).
                linked = _link_companies(db, event_id, mat_id, country)
                company_links += linked
                import_drop_events += 1
            else:
                # 9.8: UPDATE path now also refreshes company junctions
                # (see _clear_material_and_hs_junctions docstring).
                _clear_material_and_hs_junctions(db, event_id)
                _clear_company_junctions(db, event_id)
                _link_material(
                    db, event_id, mat_id,
                    hs_mapping_ids=hs_ids_by_import_signal.get((mat_id, country, year)),
                )
                linked = _link_companies(db, event_id, mat_id, country)
                company_links += linked
                import_drop_events_updated += 1

            log.info(
                "trade_signal_builder.import_decline_event",
                country=country,
                material=mat_name,
                year=year,
                drop_fraction=round(drop_fraction, 3),
                severity=severity,
                companies_linked=linked,
                was_inserted=was_inserted,
                data_coverage_warning=prev_year_data_coverage_warning,
            )

    db.commit()

    log.info(
        "trade_signal_builder.done",
        concentration_events=concentration_events,
        concentration_events_updated=concentration_events_updated,
        export_drop_events=export_drop_events,
        export_drop_events_updated=export_drop_events_updated,
        import_drop_events=import_drop_events,
        import_drop_events_updated=import_drop_events_updated,
        company_links=company_links,
    )
    return {
        "concentration_events": concentration_events,
        "concentration_events_updated": concentration_events_updated,
        "export_drop_events": export_drop_events,
        "export_drop_events_updated": export_drop_events_updated,
        "import_drop_events": import_drop_events,
        "import_drop_events_updated": import_drop_events_updated,
        "company_links": company_links,
        # Kept for backwards-compat with callers that read this key —
        # under UPSERT semantics, "skipped_existing" no longer happens;
        # what would have been skipped is now reported as *_updated.
        "skipped_existing": 0,
    }
