"""
Evidence aggregation layer: convert EventWithRelevance records into the float
inputs each component scoring function expects.

All outputs are on the 0-1.0 scale unless the function explicitly uses a
different range (financial_pressure sub-components use 0-40/0-30/0-30).

DB reads are confined to a single carve-out: ``derive_regulatory_inputs``
optionally accepts a SQLAlchemy session so it can query verified regulations
with a ``single_country_cap_pct`` metadata flag and emit synthetic
threshold-violation impacts when the company's geo concentration breaches
the cap.  All other aggregator inputs continue to arrive as pre-fetched
ORM objects from ``evidence_query.py``.  No scoring results are persisted
here — that is the orchestrator's job.

Scoring v3.0 additions:

- ``derive_material_inputs`` accepts ``material_country_events`` so events
  tagged to ``(material, country)`` pairs the company is exposed to feed
  ``trade_volatility`` even without a direct ``RiskEventCompany`` row.
- ``derive_material_inputs`` accepts an optional ``chemistry_mix`` +
  ``chemistry_intensities`` map. When present, each
  ``CompanyMaterialExposure`` is re-weighted by the chemistry-aware
  intensity of its material, so a 60% LFP / 40% NMC OEM gets material risk
  dominated by the materials it actually consumes.
- ``derive_geopolitical_inputs`` accepts ``facilities`` (facility countries
  blend into ``country_concentration``) and ``geo_events`` (events tagged
  to either source-geo or facility countries widen the export/tariff
  candidate pool).
- ``derive_regulatory_inputs`` accepts ``scope_obligations`` (regulations
  reachable via ``RegulationMaterialScope`` / ``RegulationGeographyScope``)
  and ``regulation_events`` (events tagged to applicable regulations).
- ``derive_operational_inputs`` accepts ``facilities`` (planned /
  under_construction sites lift ``structural_dependency``) and
  ``facility_country_events``.
- ``derive_propagation_inputs`` is new — it converts a supplier-chain BFS
  result + a ``{company_id -> CompanyScore}`` map into the tuple list the
  ``score_propagation`` pillar function consumes.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Optional

import structlog

from app.models.company import CompanyMaterialExposure, CompanyScore
from app.services.scoring.decay import RiskCategory, compute_recency_multiplier
from app.services.scoring.event_impact import (
    apply_scope_severity_multiplier,
    compute_event_impact,
    relevance_score_to_multiplier,
)
from app.services.scoring.evidence_query import EventWithRelevance, SupplierEdge

log = structlog.get_logger(__name__)

_HIGH_CONCENTRATION_GEOS = frozenset({"CN", "CD", "RU"})

_MAX_EVENT_IMPACT = 1.56

# Materials that the company holds a CompanyMaterialExposure on but that no
# active chemistry uses get this fractional weight rather than zero. Keeps
# legacy / non-battery exposures visible without dominating the pillar.
_CHEMISTRY_BASELINE_UNMATCHED = 0.10

# Downweight applied to a regulation event's impact when the regulation
# declares one or more ``targeted_country`` geography scopes (e.g. UFLPA → CN
# for Xinjiang, EU Conflict Minerals → CD for DRC, OFAC → CN/RU/KP/IR) and
# the company's source-geography set (material source countries ∪ facility
# countries) has NO overlap with those targeted countries.
#
# Rationale: a country-targeted regulation primarily bites companies that
# actually source from (or operate in) the targeted jurisdiction. A company
# with no exposure there still has residual tier-2 / due-diligence
# compliance burden, so the event is NOT zeroed out — it is attenuated to
# 30% of its nominal impact. Regulations with no ``targeted_country`` scope
# (the common case — most non-sanctions instruments) are unaffected.
#
# Referenced by the methodology doc as the "conditional scope intersection"
# factor (EUR-Lex Section 5 Option C).
_CONDITIONAL_SCOPE_DOWNWEIGHT = 0.30


# ---------------------------------------------------------------------------
# Single-country-cap synthetic event trigger (Idea B, 2026-05-19)
# ---------------------------------------------------------------------------
#
# Regulations that declare a quantitative single-country concentration cap
# (e.g., CRMA_2024's 65% no-single-third-country rule for strategic raw
# materials) opt in to the synthetic-event path by setting
# ``metadata_json["single_country_cap_pct"]`` on the Regulation row. When
# a company's source-geography mix violates that cap for a material in the
# regulation's scope, the regulatory pillar receives an additional synthetic
# event_impact alongside its real event_impacts. This makes a passive
# obligation (CRMA contributes obligation_uplift) actively reactive to the
# company's actual concentration profile.
#
# Synthetic-impact composition (matches ``compute_event_impact`` semantics):
#   severity * confidence * recency_multiplier * relevance_multiplier
#     - severity:    regulation_severity_weight(reg) if importable,
#                    else _DEFAULT_REG_SEVERITY_FOR_SYNTHETIC
#     - confidence:  0.9  (partner-curated cap value, near-certain)
#     - recency:     1.0  (synthetic events are present-tense — the
#                    violation IS the present state, no decay applies)
#     - relevance:   1.30 (max — partner-curated scope, highest
#                    attribution mechanism in the calibration band)
# Upper bound: 0.70 * 0.9 * 1.0 * 1.30 = 0.819 per violation.
_THRESHOLD_VIOLATION_CONFIDENCE = 0.9
_THRESHOLD_VIOLATION_RECENCY = 1.0
_THRESHOLD_VIOLATION_RELEVANCE = 1.30
_DEFAULT_REG_SEVERITY_FOR_SYNTHETIC = 0.70  # matches SEVERITY_BY_STATUS["effective"]

# EU member states (+ "EU" itself as a bloc identifier) — used to identify
# "EU-side" sourcing that should be EXCLUDED from CRMA-style single-third-
# country cap checks. A company sourcing 80% of its lithium from within the
# EU is NOT violating CRMA's single-non-EU-country cap; the regulation
# targets dependency on a single THIRD country. The seed uses "EU" as a
# single ISO code for the regulation's jurisdiction scope, but
# RegulationGeographyScope.country_code is permitted to carry individual
# ISO2 codes too, so we treat both forms as EU-side.
_EU_MEMBER_STATES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR",
    "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU", "MT", "NL",
    "PL", "PT", "RO", "SK", "SI", "ES", "SE", "EU",
})


def _event_date_as_date(ew: EventWithRelevance, fallback: date) -> date:
    if ew.event.event_date is None:
        return fallback
    return ew.event.event_date.date()


def _safe_severity(ew: EventWithRelevance) -> float:
    v = ew.event.severity_score
    if v is None:
        log.warning("evidence_aggregator.missing_severity", event_id=ew.event.id)
        return 0.5
    return float(v)


def _safe_confidence(ew: EventWithRelevance) -> float:
    v = ew.event.confidence_score
    if v is None:
        log.warning("evidence_aggregator.missing_confidence", event_id=ew.event.id)
        return 0.5
    return float(v)


def _normalise_exposure_score(raw: float) -> float:
    return min(1.0, max(0.0, raw))


def _impact(
    ew: EventWithRelevance,
    category: RiskCategory,
    as_of_date: date,
    effective_date: Optional[date] = None,
) -> float:
    ev_date = _event_date_as_date(ew, as_of_date)
    recency = compute_recency_multiplier(
        category, ev_date, as_of_date, effective_date=effective_date
    )
    # Apply the scope-type severity multiplier BEFORE calling
    # ``compute_event_impact``.  When ``ew.scope_type`` is one of the
    # designations in ``SCOPE_SEVERITY_MULTIPLIER`` (set by the EUR-Lex
    # ingester from ``RegulationMaterialScope.scope_type``), severity is
    # scaled — ``banned`` 1.50× through ``disclosure_required`` 0.50×,
    # clamped to 1.0.  When it is ``None`` (non-regulation events, geography
    # and company anchored queries, pre-migration rows), severity is
    # unchanged so backwards compatibility holds.
    severity = apply_scope_severity_multiplier(
        _safe_severity(ew), ew.scope_type
    )
    return compute_event_impact(
        severity=severity,
        confidence=_safe_confidence(ew),
        recency_multiplier=recency,
        # ew.relevance_score is on [0, 1]; map to the [0.70, 1.30]
        # multiplier domain compute_event_impact validates against.
        relevance_multiplier=relevance_score_to_multiplier(ew.relevance_score),
    )


def _dedup_events(
    *event_lists: list[EventWithRelevance],
) -> list[EventWithRelevance]:
    """Union of EventWithRelevance lists by event id, keeping max relevance."""
    by_id: dict[int, EventWithRelevance] = {}
    for lst in event_lists:
        for ew in lst:
            existing = by_id.get(ew.event.id)
            if existing is None or ew.relevance_score > existing.relevance_score:
                by_id[ew.event.id] = ew
    return list(by_id.values())


def _chemistry_weight_for_material(
    material_id: int,
    chemistry_mix: dict[int, float],
    chemistry_intensities: dict[int, dict[int, float]],
) -> float:
    """``sum_over_chemistries(share[c] * intensities[c].get(material_id, 0))``."""
    weight = 0.0
    for chem_id, share in chemistry_mix.items():
        intensity = chemistry_intensities.get(chem_id, {}).get(material_id)
        if intensity is None:
            continue
        weight += share * intensity
    return weight


# ---------------------------------------------------------------------------
# Public aggregation functions
# ---------------------------------------------------------------------------

def derive_material_inputs(
    material_exposures: list[CompanyMaterialExposure],
    trade_events: list[EventWithRelevance],
    as_of_date: date,
    *,
    material_country_events: Optional[list[EventWithRelevance]] = None,
    chemistry_mix: Optional[dict[int, float]] = None,
    chemistry_intensities: Optional[dict[int, dict[int, float]]] = None,
) -> tuple[float, float, float]:
    """
    Returns (criticality, concentration, trade_volatility) each on [0, 1.0].

    criticality:
        Average exposure_score across all materials. When ``chemistry_mix`` and
        ``chemistry_intensities`` are both present, each exposure is re-weighted
        by its material's chemistry-aware intensity — so a 100% LFP OEM weights
        cobalt near zero and Li/Fe/P near full strength.
        Conservative default 0.5 if no exposure records exist.

    concentration:
        Proportion of exposures with source_geography in HIGH_CONCENTRATION_GEOS.
        When chemistry weighting is active, the proportion is computed over
        chemistry-relevant exposures (per-exposure weight ≥ baseline).
        Defaults to 0.5 if no exposures have a source_geography set (data gap).

    trade_volatility:
        Average event_impact of GEOPOLITICAL_TRADE events (company-tagged ∪
        material-country-tagged), normalised to [0, 1.0] by the theoretical
        max impact (1.56). Default 0.3 if no events. Trade volatility is NOT
        chemistry-weighted — a lithium tariff is a lithium tariff regardless
        of the buyer's recipe.
    """
    use_chem = chemistry_mix is not None and chemistry_intensities is not None

    def _mat_weight(mat_id: int) -> float:
        if not use_chem:
            return 1.0
        w = _chemistry_weight_for_material(
            mat_id, chemistry_mix, chemistry_intensities  # type: ignore[arg-type]
        )
        return w if w > 0 else _CHEMISTRY_BASELINE_UNMATCHED

    # --- criticality ---
    if not material_exposures:
        log.warning("evidence_aggregator.no_material_exposures")
        criticality = 0.5
    else:
        weighted_num = 0.0
        weight_denom = 0.0
        for e in material_exposures:
            w = _mat_weight(e.material_id)
            weighted_num += _normalise_exposure_score(e.exposure_score) * w
            weight_denom += w
        criticality = (
            weighted_num / weight_denom if weight_denom > 0 else 0.5
        )

    # --- concentration ---
    geo_tagged = [e for e in material_exposures if e.source_geography]
    if not geo_tagged:
        concentration = 0.5
    else:
        hcg_weight = 0.0
        total_weight = 0.0
        for e in geo_tagged:
            w = _mat_weight(e.material_id)
            total_weight += w
            if e.source_geography in _HIGH_CONCENTRATION_GEOS:
                hcg_weight += w
        concentration = hcg_weight / total_weight if total_weight > 0 else 0.5

    # --- trade_volatility ---
    all_trade = _dedup_events(
        trade_events,
        material_country_events or [],
    )
    if not all_trade:
        trade_volatility = 0.3
    else:
        impacts = [
            _impact(ew, RiskCategory.GEOPOLITICAL_TRADE, as_of_date)
            for ew in all_trade
        ]
        trade_volatility = min(
            1.0, sum(impacts) / len(impacts) / _MAX_EVENT_IMPACT
        )

    return criticality, concentration, trade_volatility


def derive_geopolitical_inputs(
    trade_events: list[EventWithRelevance],
    material_exposures: list[CompanyMaterialExposure],
    as_of_date: date,
    *,
    facilities: Optional[list] = None,
    geo_events: Optional[list[EventWithRelevance]] = None,
) -> tuple[float, float, float]:
    """
    Returns (country_concentration, export_restriction_exposure, tariff_exposure)
    each on [0, 1.0].

    country_concentration:
        50% from material source-geography HCG share, 50% from facility country
        HCG share. Either side defaults to 0.5 when no signal exists.

    export_restriction_exposure / tariff_exposure:
        Average event_impact across the union of company-tagged trade events
        and ``geo_events`` (events tagged via ``RiskEventGeography`` to either
        source-geo or facility countries), classified by subtype/keyword.
    """
    # Source-geography HCG share
    geo_tagged = [e for e in material_exposures if e.source_geography]
    if geo_tagged:
        hcg_count = sum(
            1
            for e in geo_tagged
            if e.source_geography in _HIGH_CONCENTRATION_GEOS
        )
        source_share = hcg_count / len(geo_tagged)
    else:
        source_share = 0.5

    # Facility-country HCG share
    facilities = facilities or []
    if facilities:
        fac_total = len(facilities)
        fac_hcg = sum(
            1 for f in facilities if f.country in _HIGH_CONCENTRATION_GEOS
        )
        facility_share = fac_hcg / fac_total
    else:
        facility_share = 0.5

    country_concentration = 0.5 * source_share + 0.5 * facility_share

    # Widen event pool with geo-tagged events
    all_geo = _dedup_events(trade_events, geo_events or [])

    export_events: list[EventWithRelevance] = []
    tariff_events: list[EventWithRelevance] = []

    for ew in all_geo:
        subtype = ew.event.event_subtype or ""  # typed col (migration 040)
        text = (ew.event.title or "").lower()

        if subtype == "EXPORT_RESTRICTION" or (
            "export" in text
            and ("restrict" in text or "ban" in text or "control" in text)
        ):
            export_events.append(ew)
        elif subtype in ("TARIFF", "TRADE_POLICY") or (
            "tariff" in text or "trade policy" in text or "section 301" in text
        ):
            tariff_events.append(ew)

    def _avg_impact(events: list[EventWithRelevance]) -> float:
        if not events:
            return 0.0
        impacts = [
            _impact(ew, RiskCategory.GEOPOLITICAL_TRADE, as_of_date)
            for ew in events
        ]
        return min(1.0, sum(impacts) / len(impacts) / _MAX_EVENT_IMPACT)

    return (
        country_concentration,
        _avg_impact(export_events),
        _avg_impact(tariff_events),
    )


def _compute_threshold_violation_impacts(
    db,
    exposures: list[CompanyMaterialExposure],
    as_of_date: date,
) -> list[float]:
    """Compute synthetic event_impacts for single-country-cap breaches.

    Scans verified regulations with ``metadata_json["single_country_cap_pct"]``
    set (currently CRMA_2024 at 65). For each such regulation, for each
    material in the regulation's ``RegulationMaterialScope``, the helper:

      1. Sums the company's exposure across all source-geography values for
         that material to get a denominator.
      2. Excludes "EU-side" sourcing (countries in :data:`_EU_MEMBER_STATES`
         or ``"EU"`` itself) for any regulation whose jurisdiction-scope
         country_codes intersect the EU block — CRMA's cap targets
         dependency on a single THIRD country, not on the EU itself.
         Non-EU-jurisdiction regulations that opt into the cap mechanism
         use a generic "exclude jurisdiction countries" rule.
      3. For each remaining country, computes share = country_exposure /
         total_exposure and emits a synthetic impact when share exceeds
         ``cap_pct / 100``.

    Severity uses ``regulation_severity_weight(reg)`` when importable so
    partner-curated per-regulation severity overrides flow through; falls
    back to :data:`_DEFAULT_REG_SEVERITY_FOR_SYNTHETIC` (0.70 = effective
    default) otherwise.

    Edge cases:
      * empty ``exposures``                         → []
      * no regulations with single_country_cap_pct  → []
      * company sourcing only from EU-side          → no violations possible
      * cap_pct value not coercible to float        → regulation skipped
      * material with zero total exposure           → skipped (no denominator)
    """
    if not exposures:
        return []

    # Local imports keep evidence_aggregator import-time light and avoid
    # cycles (eurlex.py pulls in heavy ingestion dependencies).
    from sqlalchemy.orm import selectinload

    from app.models.regulatory import (
        Regulation,
        RegulationGeographyScope,
        RegulationMaterialScope,
    )

    try:
        from app.services.ingestion.eurlex import regulation_severity_weight
    except Exception:  # pragma: no cover - defensive
        regulation_severity_weight = None  # type: ignore[assignment]

    from sqlalchemy import select

    stmt = (
        select(Regulation)
        .where(Regulation.verified.is_(True))
        .options(
            selectinload(Regulation.material_scopes),
            selectinload(Regulation.geography_scopes),
        )
    )
    regs = list(db.execute(stmt).scalars().all())

    # Filter to regulations carrying a single_country_cap_pct flag.
    capped: list[tuple[Regulation, float, set[int], set[str]]] = []
    for reg in regs:
        meta = reg.metadata_json or {}
        if not isinstance(meta, dict):
            continue
        raw_cap = meta.get("single_country_cap_pct")
        if raw_cap is None:
            continue
        try:
            cap_pct = float(raw_cap)
        except (TypeError, ValueError):
            log.warning(
                "evidence_aggregator.invalid_single_country_cap",
                regulation_key=reg.regulation_key,
                raw_value=raw_cap,
            )
            continue

        scoped_material_ids: set[int] = {
            ms.material_id for ms in reg.material_scopes
        }
        # Excluded countries = ISO2 codes from this regulation's
        # ``jurisdiction``-typed geography scopes. For CRMA the seed sets
        # one row with country_code="EU", which we expand to the full EU
        # member set via _EU_MEMBER_STATES below.
        jurisdiction_codes: set[str] = {
            (gs.country_code or "").upper()
            for gs in reg.geography_scopes
            if gs.scope_type == "jurisdiction" and gs.country_code
        }
        excluded: set[str] = set(jurisdiction_codes)
        # If the jurisdiction is "EU" (or any EU member), expand to the
        # full EU bloc so individual member-state source rows are also
        # treated as EU-side.
        if jurisdiction_codes & _EU_MEMBER_STATES:
            excluded |= _EU_MEMBER_STATES
        capped.append((reg, cap_pct, scoped_material_ids, excluded))

    if not capped:
        return []

    # Index exposures by material_id for O(M+E) scanning.
    by_material: dict[int, list[CompanyMaterialExposure]] = {}
    for e in exposures:
        by_material.setdefault(e.material_id, []).append(e)

    impacts: list[float] = []
    for reg, cap_pct, scoped_material_ids, excluded in capped:
        if regulation_severity_weight is not None:
            try:
                severity = regulation_severity_weight(reg)
            except Exception:  # pragma: no cover - defensive
                severity = _DEFAULT_REG_SEVERITY_FOR_SYNTHETIC
        else:
            severity = _DEFAULT_REG_SEVERITY_FOR_SYNTHETIC

        threshold = cap_pct / 100.0
        for material_id in scoped_material_ids:
            mat_exposures = by_material.get(material_id)
            if not mat_exposures:
                continue

            # Total exposure denominator across ALL source-geographies for
            # this material (including EU-side rows — CRMA is a concentration
            # ratio, so the EU-side contribution sits in the denominator
            # but cannot itself be flagged as the violating share).
            per_country: dict[str, float] = {}
            total = 0.0
            for ex in mat_exposures:
                geo = (ex.source_geography or "").upper()
                if not geo:
                    # Unattributed exposures contribute to the denominator
                    # but cannot trigger a violation on their own.
                    total += float(ex.exposure_score)
                    continue
                per_country[geo] = per_country.get(geo, 0.0) + float(
                    ex.exposure_score
                )
                total += float(ex.exposure_score)

            if total <= 0:
                continue

            for country, country_exp in per_country.items():
                if country in excluded:
                    continue
                share = country_exp / total
                if share > threshold:
                    impact = (
                        severity
                        * _THRESHOLD_VIOLATION_CONFIDENCE
                        * _THRESHOLD_VIOLATION_RECENCY
                        * _THRESHOLD_VIOLATION_RELEVANCE
                    )
                    impacts.append(impact)
                    log.debug(
                        "evidence_aggregator.threshold_violation",
                        regulation_key=reg.regulation_key,
                        material_id=material_id,
                        country_code=country,
                        observed_share=round(share, 4),
                        cap_pct=cap_pct,
                        severity=round(severity, 3),
                        synthetic_impact=round(impact, 4),
                    )

    return impacts


def derive_regulatory_inputs(
    regulatory_events: list[EventWithRelevance],
    active_obligations: list[tuple[str, float]],
    as_of_date: date,
    *,
    scope_obligations: Optional[list[tuple[str, float]]] = None,
    regulation_events: Optional[list[EventWithRelevance]] = None,
    event_targeted_countries: Optional[dict[int, set[str]]] = None,
    company_country_set: Optional[set[str]] = None,
    db=None,
    exposures: Optional[list[CompanyMaterialExposure]] = None,
) -> tuple[list[float], list[tuple[str, float]], float]:
    """
    Returns (top_event_impacts, active_obligations, policy_proximity_adjustment).

    active_obligations:
        UNION (max-weight) of ``active_obligations`` and ``scope_obligations``.
        Scope-only hits already arrive at weight 0.50 from
        ``get_regulations_scoping_company``.

    top_event_impacts:
        Computed event_impact for each regulatory event (company-tagged ∪
        regulation-tagged). regulatory_risk.py consumes the top-3 internally.

        Conditional scope intersection (Option C from EUR-Lex Section 5):
        when an event is tagged to a regulation that declares one or more
        ``targeted_country`` geography scopes (sanctions-style: UFLPA → CN,
        EU Conflict Minerals → CD, OFAC → CN/RU/KP/IR) and the company's
        ``country_set`` (material source-geographies ∪ facility countries)
        has NO overlap with those targeted countries, the event's impact is
        scaled by :data:`_CONDITIONAL_SCOPE_DOWNWEIGHT` (0.30). Regulations
        without any ``targeted_country`` scope are unaffected. An empty
        ``company_country_set`` is treated as "no overlap" — companies with
        no recorded geographies receive the downweighted impact.

    policy_proximity_adjustment:
        1.15 if any event has an effective_date within 90 days of as_of_date.
        1.0 otherwise.

    Single-country-cap synthetic events (Idea B, 2026-05-19):
        When ``db`` and ``exposures`` are both supplied, the function also
        queries verified regulations carrying
        ``metadata_json["single_country_cap_pct"]`` and appends one
        synthetic event_impact per material × non-jurisdiction country
        whose share exceeds the cap.  See
        :func:`_compute_threshold_violation_impacts` for the formula.
        Synthetic impacts are concatenated to ``top_event_impacts`` and
        flow through the existing top-3 averaging in
        :mod:`app.services.scoring.regulatory_risk`.
    """
    # Merge obligations (max weight per key)
    merged: dict[str, float] = {}
    for key, weight in active_obligations:
        merged[key] = max(merged.get(key, 0.0), weight)
    for key, weight in scope_obligations or []:
        merged[key] = max(merged.get(key, 0.0), weight)
    final_obligations = sorted(merged.items())

    all_reg = _dedup_events(regulatory_events, regulation_events or [])

    # Normalise the company country set to uppercase ISO2 for comparison
    # parity with RegulationGeographyScope.country_code (which the seed and
    # EUR-Lex ingest both store uppercase).
    if company_country_set:
        company_countries_norm: set[str] = {
            c.upper() for c in company_country_set if c
        }
    else:
        company_countries_norm = set()

    event_targeted_countries = event_targeted_countries or {}

    top_event_impacts: list[float] = []
    policy_proximity_adjustment = 1.0

    for ew in all_reg:
        effective_date: Optional[date] = None
        meta = ew.event.metadata_json or {}
        raw_eff = meta.get("effective_date")
        if raw_eff and isinstance(raw_eff, str):
            try:
                from datetime import datetime as _dt
                effective_date = _dt.fromisoformat(raw_eff).date()
            except ValueError:
                pass

        if effective_date is not None:
            days_to_effective = (effective_date - as_of_date).days
            if 0 <= days_to_effective <= 90:
                policy_proximity_adjustment = 1.15

        impact = _impact(
            ew, RiskCategory.REGULATORY_COMPLIANCE, as_of_date, effective_date
        )

        # Conditional scope intersection: downweight when the regulation
        # targets specific producer countries the company has no exposure to.
        targeted = event_targeted_countries.get(ew.event.id)
        if targeted:
            # Comparison is uppercase on both sides; the helper that builds
            # event_targeted_countries normalises country_code as well.
            if not (company_countries_norm & targeted):
                log.debug(
                    "evidence_aggregator.regulation_geo_no_overlap",
                    event_id=ew.event.id,
                    targeted_countries=sorted(targeted),
                    company_country_set=sorted(company_countries_norm),
                )
                impact *= _CONDITIONAL_SCOPE_DOWNWEIGHT

        top_event_impacts.append(impact)

    # Single-country-cap synthetic events (Idea B). Appended AFTER real
    # event impacts so the existing top-3 averaging in
    # ``regulatory_risk.score_regulatory_profile`` treats them as
    # first-class peers — a high-severity violation can displace a weaker
    # real event from the top-3.
    if db is not None and exposures:
        synthetic = _compute_threshold_violation_impacts(
            db, exposures, as_of_date
        )
        if synthetic:
            log.info(
                "evidence_aggregator.threshold_violations_detected",
                count=len(synthetic),
                max_impact=round(max(synthetic), 4),
            )
            top_event_impacts.extend(synthetic)

    return top_event_impacts, final_obligations, policy_proximity_adjustment


def derive_operational_inputs(
    operational_events: list[EventWithRelevance],
    as_of_date: date,
    *,
    facilities: Optional[list] = None,
    facility_country_events: Optional[list[EventWithRelevance]] = None,
) -> tuple[float, list[float]]:
    """
    Returns (structural_dependency, weighted_event_impacts).

    structural_dependency:
        max(event-derived signal, 0.4 × share_of_non_operating_facilities).
        A planned / under_construction facility footprint signals
        capacity-not-yet-online dependency. Default 0.3 when no signals exist.

    weighted_event_impacts:
        event_impact for each operational event (company-tagged ∪
        facility-country-tagged).
    """
    struct_events: list[EventWithRelevance] = []
    for ew in operational_events:
        subtype = ew.event.event_subtype or ""  # typed col (migration 040)
        text = (ew.event.title or "").lower()
        if subtype in ("SINGLE_SOURCE", "CAPACITY_CONSTRAINT") or (
            "single source" in text
            or "single-source" in text
            or "capacity constraint" in text
        ):
            struct_events.append(ew)

    if struct_events:
        event_struct_dep = sum(
            _safe_severity(ew) for ew in struct_events
        ) / len(struct_events)
    else:
        event_struct_dep = 0.3

    # Facility-derived signal: share of non-operating facilities
    facilities = facilities or []
    if facilities:
        non_op = sum(
            1
            for f in facilities
            if f.status in ("planned", "under_construction")
        )
        facility_struct_dep = 0.4 * (non_op / len(facilities))
    else:
        facility_struct_dep = 0.0

    structural_dependency = max(event_struct_dep, facility_struct_dep)

    all_op = _dedup_events(operational_events, facility_country_events or [])
    weighted_event_impacts = [
        _impact(ew, RiskCategory.OPERATIONAL, as_of_date) for ew in all_op
    ]

    return structural_dependency, weighted_event_impacts


def derive_financial_inputs(
    filing_events: list[EventWithRelevance],
) -> tuple[float, float, float, int]:
    """
    Returns (base_filing_signal, leverage_warning_bonus, liquidity_stress_bonus,
    filing_count).
    """
    filing_count = len(filing_events)
    if not filing_events:
        return 0.0, 0.0, 0.0, 0

    avg_severity = sum(_safe_severity(ew) for ew in filing_events) / filing_count
    base_filing_signal = min(40.0, avg_severity * 40.0)

    # SEC subtype sets are the canonical source — defined alongside the
    # SEC ingester's filing-type map so adding a new subtype to a bucket
    # is one edit in one place.  Aspirational vocabulary (LEVERAGE_WARNING,
    # COVENANT_STRESS, GOING_CONCERN, CASH_RUNWAY, CAPEX_CUT) is preserved
    # inside those sets for any non-SEC ingester that eventually emits
    # them.  See app/services/ingestion/sec_subtype_map.py.
    from app.services.ingestion.sec_subtype_map import (
        LEVERAGE_SUBTYPES,
        LIQUIDITY_SUBTYPES,
    )

    leverage_sum = 0.0
    for ew in filing_events:
        subtype = ew.event.event_subtype or ""  # typed col (migration 040)
        text = (ew.event.title or "").lower()
        if subtype in LEVERAGE_SUBTYPES or (
            "leverage" in text or "covenant" in text or "debt" in text
        ):
            leverage_sum += _safe_severity(ew)
    leverage_warning_bonus = min(30.0, leverage_sum)

    liquidity_sum = 0.0
    for ew in filing_events:
        subtype = ew.event.event_subtype or ""  # typed col (migration 040)
        text = (ew.event.title or "").lower()
        if subtype in LIQUIDITY_SUBTYPES or (
            "going concern" in text
            or "cash runway" in text
            or "suspended" in text
            or "capex" in text
        ):
            liquidity_sum += _safe_severity(ew)
    liquidity_stress_bonus = min(30.0, liquidity_sum)

    return base_filing_signal, leverage_warning_bonus, liquidity_stress_bonus, filing_count


# ---------------------------------------------------------------------------
# Propagation aggregator (Step 4b — supply-chain rollup)
# ---------------------------------------------------------------------------

# Conservative volume default for supplier edges with NULL volume_share_pct.
# Unknown-volume edges should contribute meaningfully but never dominate; 10%
# matches the convention used elsewhere in the pipeline for "data-gap" inputs.
_PROPAGATION_DEFAULT_VOLUME_SHARE = 0.10


def derive_propagation_inputs(
    supplier_chain: list[SupplierEdge],
    latest_scores: dict[uuid.UUID, CompanyScore],
) -> list[tuple[float, float, int]]:
    """Convert (chain, scores) into the tuple list ``score_propagation`` wants.

    Each output tuple is ``(supplier_overall_score, edge_volume_share, depth)``
    where ``edge_volume_share`` falls back to
    :data:`_PROPAGATION_DEFAULT_VOLUME_SHARE` when the cumulative share is
    unknown. Supplier edges with no persisted ``CompanyScore`` are dropped
    from the input set; the orchestrator surfaces the count in
    ``rationale_json.signals_used`` so the UI can show "N suppliers skipped:
    no score yet".
    """
    out: list[tuple[float, float, int]] = []
    for edge in supplier_chain:
        score = latest_scores.get(edge.supplier_id)
        if score is None or score.overall_risk_score is None:
            continue
        share = (
            edge.cumulative_volume_share
            if edge.cumulative_volume_share is not None
            else _PROPAGATION_DEFAULT_VOLUME_SHARE
        )
        out.append((float(score.overall_risk_score), float(share), edge.depth))
    return out
