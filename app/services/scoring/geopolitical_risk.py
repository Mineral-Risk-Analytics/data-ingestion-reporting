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

# NOTE: apply_wgi_governance_overlay (the JRC WGI governance overlay)
# was removed in 4.1 — governance moved into the concentration pillar as
# a production-share-anchored instability amplifier (spec §3), and the
# geopolitical overlay was dropped to avoid double-counting.
