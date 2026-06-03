"""Diagnose GTA NFI / IFI events to inform multiplier calibration.

Run against the live dev DB:

    cd /path/to/battery-data-intelligence-engine
    python "Automotive Data Solutions/diagnose_gta_nfi_ifi.py"

Current state (pre-recalibration):
    NFI = National Financial Institution  → 1.0× multiplier (placeholder)
    IFI = International Financial Institution → 1.0× multiplier (placeholder)

The placeholder was set 2026-05-06 when NFI/IFI events had ``event_subtype=NULL``
and skipped HS-node sub-scores entirely.  G-Cov-3 (2026-05-09) wired the
EXPORT_SUBSIDY subtype for 11 financial-support intervention types, and the
new Geopolitical pillar 4-component profile includes
``production_subsidy_distortion`` at 0.10 weight.  The multiplier now
matters.

This script reports the empirical distribution needed to make a calibrated
decision rather than guessing:

  * Count of NFI / IFI events
  * Top intervention_types within each
  * event_subtype distribution (sanity check that G-Cov-3 is firing)
  * Implementing country breakdown (China-dominated vs distributed?)
  * Severity_score distribution (what's the baseline before multiplier?)
  * Material attribution counts (how many materials see these events?)

Outputs land in Automotive Data Solutions/ as CSVs alongside this script.
"""

from __future__ import annotations

import csv
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    from sqlalchemy import func, select
    from sqlalchemy.orm import Session

    from app.db.session import get_session_factory
    from app.models.regulatory import (
        RiskEvent,
        RiskEventGeography,
        RiskEventMaterial,
    )
    from app.models.source import Source
    from app.models.documents import SourceDocument
    from app.models.supply import Material

    OUT_DIR = Path(__file__).resolve().parent

    print("=" * 64)
    print("GTA NFI/IFI multiplier calibration — empirical survey")
    print("=" * 64)

    SessionLocal = get_session_factory()
    s: Session = SessionLocal()
    try:
        # Find the GTA source
        gta_src = s.scalar(
            select(Source).where(Source.name == "Global Trade Alert")
        )
        if gta_src is None:
            print(
                "\nNo Source row named 'Global Trade Alert' found. Run "
                "`bdi-ingest ingest-gta --local-file <path>` first.",
                file=sys.stderr,
            )
            return 1

        # All GTA events have source_document.source_id == gta_src.id
        # and metadata_json carries implementation_level.
        gta_events = s.execute(
            select(RiskEvent, SourceDocument)
            .join(
                SourceDocument,
                SourceDocument.id == RiskEvent.source_document_id,
            )
            .where(SourceDocument.source_id == gta_src.id)
        ).all()

        if not gta_events:
            print(
                "\nNo GTA events found in risk_events. Run "
                "`bdi-ingest ingest-gta --local-file <path>` first.",
                file=sys.stderr,
            )
            return 0

        print(f"\nFound {len(gta_events)} total GTA events.\n")

        # Bucket by implementation_level
        by_level: dict[str | None, list] = {}
        for ev, _doc in gta_events:
            meta = ev.metadata_json or {}
            level = meta.get("implementation_level")
            if level:
                level = str(level).strip().lower()
            by_level.setdefault(level, []).append(ev)

        # Top-line counts
        print("Implementation-level distribution:")
        print("-" * 64)
        print(f"{'Level':<24} {'Count':>8}")
        print("-" * 64)
        for level, evs in sorted(by_level.items(), key=lambda kv: -len(kv[1])):
            display = level or "(missing)"
            print(f"{display:<24} {len(evs):>8}")
        print("-" * 64)
        print(f"{'TOTAL':<24} {len(gta_events):>8}")

        # Focus on NFI / IFI
        nfi_events = by_level.get("nfi", [])
        ifi_events = by_level.get("ifi", [])

        if not (nfi_events or ifi_events):
            print(
                "\nNo NFI or IFI events found.  Either the GTA CSV has "
                "no rows of these types, or the implementation_level "
                "column hasn't been parsed correctly.  No calibration "
                "needed under current data."
            )
            return 0

        # Material attribution counts
        material_links_by_event = {}
        if nfi_events or ifi_events:
            event_ids = [ev.id for ev in nfi_events + ifi_events]
            rows = s.execute(
                select(
                    RiskEventMaterial.risk_event_id,
                    RiskEventMaterial.material_id,
                    Material.canonical_name,
                )
                .join(Material, Material.id == RiskEventMaterial.material_id)
                .where(RiskEventMaterial.risk_event_id.in_(event_ids))
            ).all()
            for tf_id, mat_id, mat_name in rows:
                material_links_by_event.setdefault(tf_id, []).append(mat_name)

        # Geography
        geo_by_event = {}
        if nfi_events or ifi_events:
            event_ids = [ev.id for ev in nfi_events + ifi_events]
            rows = s.execute(
                select(
                    RiskEventGeography.risk_event_id,
                    RiskEventGeography.country_code,
                )
                .where(
                    RiskEventGeography.risk_event_id.in_(event_ids),
                    RiskEventGeography.geography_context == "primary",
                )
            ).all()
            for tf_id, cc in rows:
                geo_by_event.setdefault(tf_id, []).append(cc)

        def _report_bucket(name: str, events: list) -> dict:
            print(f"\n{'='*64}")
            print(f"Bucket: {name}  (n={len(events)})")
            print(f"{'='*64}")

            if not events:
                print("(no events)")
                return {}

            # intervention_type distribution (from metadata_json)
            intervention_types: Counter[str] = Counter()
            event_subtypes: Counter[str] = Counter()
            implementing_countries: Counter[str] = Counter()
            severities: list[float] = []

            for ev in events:
                meta = ev.metadata_json or {}
                intervention_types[str(meta.get("raw_intervention_type") or "?")] += 1
                event_subtypes[str(ev.event_subtype or meta.get("event_subtype") or "(none)")] += 1
                if ev.severity_score is not None:
                    severities.append(float(ev.severity_score))
                # primary country from geography
                primaries = geo_by_event.get(ev.id, [])
                if primaries:
                    implementing_countries[primaries[0]] += 1

            # Top intervention types
            print("\nTop intervention_types:")
            for it, ct in intervention_types.most_common(10):
                print(f"  {ct:>4}  {it}")

            # event_subtype distribution — sanity check G-Cov-3 wired
            print("\nevent_subtype distribution:")
            for st, ct in event_subtypes.most_common():
                print(f"  {ct:>4}  {st}")

            # Top implementing countries
            print("\nTop implementing countries:")
            for cc, ct in implementing_countries.most_common(10):
                print(f"  {ct:>4}  {cc}")

            # Severity distribution
            if severities:
                severities.sort()
                n = len(severities)
                p25 = severities[n // 4]
                p50 = severities[n // 2]
                p75 = severities[(3 * n) // 4]
                avg = sum(severities) / n
                print("\nSeverity_score distribution:")
                print(f"  min  : {severities[0]:.3f}")
                print(f"  p25  : {p25:.3f}")
                print(f"  p50  : {p50:.3f}")
                print(f"  p75  : {p75:.3f}")
                print(f"  max  : {severities[-1]:.3f}")
                print(f"  mean : {avg:.3f}")

            # Material attribution
            material_counts: Counter[str] = Counter()
            events_with_materials = 0
            for ev in events:
                mats = material_links_by_event.get(ev.id, [])
                if mats:
                    events_with_materials += 1
                for m in mats:
                    material_counts[m] += 1
            print(f"\nEvents with material attribution: {events_with_materials} / {len(events)}")
            print(f"Top materials by event count:")
            for m, ct in material_counts.most_common(10):
                print(f"  {ct:>4}  {m}")

            return {
                "n_events": len(events),
                "intervention_types": dict(intervention_types),
                "event_subtypes": dict(event_subtypes),
                "implementing_countries": dict(implementing_countries),
                "severity_p25": severities[n // 4] if severities else None,
                "severity_p50": severities[n // 2] if severities else None,
                "severity_p75": severities[(3 * n) // 4] if severities else None,
                "severity_mean": sum(severities) / len(severities) if severities else None,
                "events_with_material_attribution": events_with_materials,
                "material_counts": dict(material_counts),
            }

        nfi_report = _report_bucket("NFI (National Financial Institution)", nfi_events)
        ifi_report = _report_bucket("IFI (International Financial Institution)", ifi_events)

        # ── Comparison row — write a CSV the user can paste back ─────────
        out_csv = OUT_DIR / "gta_nfi_ifi_survey.csv"
        with out_csv.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow([
                "bucket",
                "n_events",
                "events_with_material_attribution",
                "severity_p25",
                "severity_p50",
                "severity_p75",
                "severity_mean",
                "top_intervention_type",
                "top_implementing_country",
                "top_material",
            ])
            for name, rpt in (("NFI", nfi_report), ("IFI", ifi_report)):
                if not rpt:
                    continue

                def _top(d: dict) -> str:
                    return max(d.items(), key=lambda kv: kv[1])[0] if d else ""

                w.writerow([
                    name,
                    rpt.get("n_events", 0),
                    rpt.get("events_with_material_attribution", 0),
                    rpt.get("severity_p25"),
                    rpt.get("severity_p50"),
                    rpt.get("severity_p75"),
                    rpt.get("severity_mean"),
                    _top(rpt.get("intervention_types") or {}),
                    _top(rpt.get("implementing_countries") or {}),
                    _top(rpt.get("material_counts") or {}),
                ])
        print(f"\nWrote CSV summary: {out_csv}")

        # ── Calibration guidance ─────────────────────────────────────────
        print("\n" + "=" * 64)
        print("Calibration guidance")
        print("=" * 64)
        if nfi_report:
            nfi_top_country = max(
                (nfi_report.get("implementing_countries") or {}).items(),
                key=lambda kv: kv[1], default=("?", 0),
            )[0]
            print(
                f"NFI dominant country: {nfi_top_country}.  "
                f"If this is CN, NFI signals are dominantly state-bank funded "
                f"competitive distortion → multiplier toward 0.9-1.0 is "
                f"defensible.  If diversified across multiple economies, "
                f"NFI is more like normal industrial policy → 0.7-0.8 is "
                f"more honest.",
            )
        if ifi_report:
            ifi_top_country = max(
                (ifi_report.get("implementing_countries") or {}).items(),
                key=lambda kv: kv[1], default=("?", 0),
            )[0]
            print(
                f"\nIFI dominant country: {ifi_top_country}.  "
                f"IFI events (EIB / World Bank / EBRD / ADB) are generally "
                f"considered less distortive than single-country state aid "
                f"because they go through multilateral oversight.  WTO "
                f"Subsidies Agreement specifically exempts multilateral "
                f"development bank financing from 'actionable subsidies'.  "
                f"Multiplier around 0.5-0.7 reflects this.",
            )
        print(
            "\nKey question to answer: do these signals appear in launch-list "
            "materials?  If material attribution coverage is low (most events "
            "have no RiskEventMaterial rows), the multiplier choice has little "
            "practical effect on scoring outputs."
        )
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
