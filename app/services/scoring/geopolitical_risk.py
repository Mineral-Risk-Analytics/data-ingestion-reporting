"""Geopolitical / Trade Risk component scoring (v2) — fifth pillar at 20%.

Two scoring profiles depending on whether subsidy-distortion data is
available for the (material × geography) pair:

* **3-component (default, no subsidy data):**
  country_concentration 40%, export_restriction_exposure 35%,
  tariff_exposure 25%.

* **4-component (when subsidy data present, G-Cov-3 added 2026-05-09):**
  country_concentration 40%, export_restriction_exposure 30%,
  tariff_exposure 20%, production_subsidy_distortion 10%.

Country concentration keeps the highest weight in both profiles —
structural geographic dependency is the dominant risk for EV battery
supply chains (China ~70-80% of cell production and refining capacity
as of 2024-2026).  The subsidy term is intentionally modest (10%):
subsidies don't increase sourcing risk for the producer country
itself, but they DO distort downstream competitive dynamics in ways
the partner wanted reflected.  See docs/coverage-gap-plan-2026-05.md
§ G-Cov-3 for the partner conversation around weight calibration.
"""

from __future__ import annotations

from typing import Optional


def score_geopolitical_trade(
    country_concentration: float,
    export_restriction_exposure: float,
    tariff_exposure: float,
    production_subsidy_distortion: Optional[float] = None,
) -> float:
    """Geopolitical / Trade Risk component score (0-100).

    Args:
        country_concentration:          Share of supply chain in high-risk
                                        geographies on [0, 1].
        export_restriction_exposure:    Historical export-restriction risk
                                        for source countries on [0, 1].
        tariff_exposure:                Current tariff burden plus pending
                                        policy signals on [0, 1].
        production_subsidy_distortion:  Subsidy-event-based signal on
                                        [0, 1] reflecting state intervention
                                        in upstream production.  ``None``
                                        triggers the 3-component scoring
                                        profile (backward-compatible);
                                        any numeric value (including 0.0)
                                        triggers the 4-component profile.

    Returns:
        Component score on [0, 100].
    """
    if production_subsidy_distortion is None:
        # 3-component profile (no subsidy data — pre-G-Cov-3 behaviour)
        return min(100.0, (
            0.40 * country_concentration
            + 0.35 * export_restriction_exposure
            + 0.25 * tariff_exposure
        ) * 100)
    # 4-component profile (G-Cov-3, 2026-05-09)
    return min(100.0, (
        0.40 * country_concentration
        + 0.30 * export_restriction_exposure
        + 0.20 * tariff_exposure
        + 0.10 * production_subsidy_distortion
    ) * 100)


# ---------------------------------------------------------------------------
# Step 3 (2026-06) — JRC-aligned WGI governance overlay
# ---------------------------------------------------------------------------
# Background
# ----------
# Steps 1 / 2A / 2B addressed the *magnitude* of the concentration signal
# (HHI cliffs, sparse-coverage bridges) but said nothing about *who*
# controls the supply.  DRC at 75% production share carries materially
# different structural risk than Australia at the same share.
#
# JRC's published Critical Raw Materials methodology resolves this by
# overlaying World Bank Worldwide Governance Indicators on the
# concentration math:
#
#     Supply Risk = HHI × Σ_c [ share_c × (1 - WGI_c_normalised) ]
#
# Our scoring is per-(material × geography), so we adapt: each country's
# ``country_concentration`` sub-input gets discounted by ITS OWN
# governance quality.  Mathematically:
#
#     country_concentration_adjusted =
#         country_concentration_raw × (1 - α × WGI_c_normalised)
#
# Where α = 0.5 is the governance weight — matches JRC's effective
# 50/50 multiplicative split (HHI and WGI carry equal weight in their
# published formula).
#
# Calibration sense-check (all six WGI dimensions averaged into a
# 0-100 percentile rank, then divided by 100 for WGI_normalised):
#
#     Country   ≈ mean WGI %   Multiplier (α=0.5)
#     AU                ~93              0.535   nearly halved
#     US                ~78              0.610   modest discount
#     CL                ~70              0.650   modest discount
#     CN                ~46              0.770   small discount
#     ID                ~45              0.775   small discount
#     RU                ~22              0.890   tiny discount
#     CD (DRC)          ~13              0.935   barely any discount
#
# Net effect: the gap between well-governed and poorly-governed producers
# widens — which is the structural story JRC's methodology is trying to
# capture.
# ---------------------------------------------------------------------------

# JRC-aligned governance weight.  At α=0.5 the maximum discount applied
# to country_concentration is 50% (achieved when WGI_normalised = 1.0,
# i.e. perfectly-governed country).  At α=0.0 the overlay is inert.
# Increasing α above 0.5 would mean a perfectly-governed country has
# zero concentration risk regardless of share — too aggressive given
# data uncertainty.
_WGI_GOVERNANCE_WEIGHT: float = 0.5

# Minimum number of WGI dimensions present (of 6) for a country's
# ``composite_pct`` to be considered usable.  WGI publishes most rows
# with all 6 populated, but some small / contested jurisdictions have
# partial coverage.  Below this threshold we treat the country as
# "no governance signal" and apply no overlay rather than risk a
# spurious discount.
_WGI_MIN_DIMENSIONS_FOR_OVERLAY: int = 4


def apply_wgi_governance_overlay(
    *,
    country_concentration_raw: float,
    wgi_composite_pct: Optional[float],
    wgi_n_dimensions_present: Optional[int],
) -> tuple[float, dict]:
    """JRC-aligned per-country governance discount on country_concentration.

    Pure function — caller pre-fetches the country's WGI row (typically
    once per geography_code) and passes the percentile composite + the
    data-coverage count.  Returns the adjusted concentration value and a
    diagnostic dict the caller stores in rationale_json so the partner
    UI can explain "country_concentration discounted Nx because the
    sourcing country has WGI percentile Y".

    Args:
        country_concentration_raw: 0-1 share-of-supply signal for the
            country being scored, prior to any governance overlay.
        wgi_composite_pct: Mean of the six WGI percentile ranks for the
            country, on [0, 100].  ``None`` skips the overlay entirely
            (no data, no adjustment).
        wgi_n_dimensions_present: How many of the six dimensions had
            data when the composite was computed.  Below
            ``_WGI_MIN_DIMENSIONS_FOR_OVERLAY`` the overlay is skipped
            (treated as insufficient signal).

    Returns:
        ``(adjusted_score, diagnostic_dict)`` — diagnostic always
        populated for shape stability.
    """
    diag: dict = {
        "applied":               False,
        "reason":                None,
        "wgi_composite_pct":     wgi_composite_pct,
        "wgi_n_dimensions":      wgi_n_dimensions_present,
        "weight_alpha":          _WGI_GOVERNANCE_WEIGHT,
        "multiplier":            1.0,
        "country_concentration_raw":      round(country_concentration_raw, 6),
        "country_concentration_adjusted": round(country_concentration_raw, 6),
    }

    if wgi_composite_pct is None:
        diag["reason"] = "no_wgi_signal_for_country"
        return country_concentration_raw, diag

    if (
        wgi_n_dimensions_present is not None
        and wgi_n_dimensions_present < _WGI_MIN_DIMENSIONS_FOR_OVERLAY
    ):
        diag["reason"] = "insufficient_wgi_dimensions"
        return country_concentration_raw, diag

    # Normalise percentile rank to [0, 1].  Clamp defensively in case
    # the source publishes a value just above 100 (rounding artifacts
    # have been seen historically).
    wgi_normalised = max(0.0, min(1.0, wgi_composite_pct / 100.0))
    multiplier = 1.0 - _WGI_GOVERNANCE_WEIGHT * wgi_normalised
    adjusted = country_concentration_raw * multiplier

    diag.update({
        "applied":    True,
        "reason":     "jrc_aligned_wgi_overlay_applied",
        "multiplier": round(multiplier, 4),
        "country_concentration_adjusted": round(adjusted, 6),
    })
    return adjusted, diag
