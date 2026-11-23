"""One-off runner for the MCS PDF LLM section locator.

Validates that the LLM locator produces sensible output against the real
MCS 2026 PDF before we wire it into `MCSPdfParser.parse()` as the primary
path.  Not a long-term CLI surface — just a way to see it work.

Usage
-----
    # Default: read materials from DB, write cache to data/mcs2026_locator_result.json
    uv run python scripts/run_mcs_locator.py --pdf /path/to/mcs2026.pdf

    # Use a hardcoded material list (skips DB connection)
    uv run python scripts/run_mcs_locator.py --pdf /path/to/mcs2026.pdf --materials hardcoded

    # Force a re-run even if data/mcs2026_locator_result.json exists
    uv run python scripts/run_mcs_locator.py --pdf /path/to/mcs2026.pdf --force-refresh

Prerequisites
-------------
- ANTHROPIC_API_KEY env var set
- `uv add anthropic` (one-time)
- `mcs2026.pdf` somewhere on disk
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Load .env at the project root so ANTHROPIC_API_KEY (and any other env
# vars stored there) are visible to the Anthropic SDK.  Done before any
# other project imports so config-loading code sees the same env state.
try:
    from dotenv import load_dotenv
    _project_root = Path(__file__).resolve().parent.parent
    load_dotenv(_project_root / ".env")
except ImportError:
    # python-dotenv not installed — script will rely on env vars set in
    # the shell directly.  No-op here; we surface a clearer error below
    # if the API key is genuinely missing.
    pass


# ---------------------------------------------------------------------------
# Hardcoded material list (used when --materials hardcoded)
# Should match `materials.canonical_name` for the 27 battery materials we
# expect MCS to cover.  If you've updated the materials table, prefer
# `--materials db` to fetch from there.
# ---------------------------------------------------------------------------
_HARDCODED_MATERIALS: list[str] = [
    "Aluminum",
    "Antimony",
    "Boron",
    "Chromium",
    "Cobalt",
    "Copper",
    "Dysprosium",
    "Fluorspar",
    "Gallium",
    "Germanium",
    "Indium",
    "Iron Ore",
    "Lithium",
    "Magnesium",
    "Manganese",
    "Molybdenum",
    "Natural Graphite",
    "Neodymium",
    "Nickel",
    "Platinum-Group Metals",
    "Praseodymium",
    "Rare Earth Elements",
    "Silicon (Anode Grade)",
    "Silver",
    "Sodium",
    "Tantalum",
    "Terbium",
    "Tin",
    "Titanium",
    "Tungsten",
    "Vanadium",
    "Zinc",
    "Zirconium",
]


def _materials_from_db() -> list[str]:
    """Fetch canonical_name list from the materials table."""
    # Imported lazily so `--materials hardcoded` works even when the DB
    # isn't reachable.
    from sqlalchemy import select
    from app.db.session import get_session_factory
    from app.models.supply import Material

    session = get_session_factory()()
    try:
        names = list(
            session.scalars(
                select(Material.canonical_name).order_by(Material.canonical_name)
            ).all()
        )
        return names
    finally:
        session.close()


def _read_pdf_text(pdf_path: Path) -> str:
    """Extract full PDF text using the same join convention `MCSPdfParser` uses."""
    import pdfplumber

    print(f"Reading PDF: {pdf_path}")
    with pdfplumber.open(str(pdf_path)) as pdf:
        pages = [(p.extract_text() or "") for p in pdf.pages]
    print(f"  {len(pages)} pages extracted")
    combined = "\n\n--- PAGE BREAK ---\n\n".join(pages)
    print(f"  {len(combined.splitlines())} lines, {len(combined):,} characters")
    return combined


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--pdf", type=Path, required=True, help="Path to mcs2026.pdf")
    p.add_argument("--year", type=int, default=2026, help="MCS edition year (used for cache filename)")
    p.add_argument(
        "--materials",
        choices=["db", "hardcoded"],
        default="db",
        help="Where to source canonical material names from",
    )
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=Path("data"),
        help="Directory for the locator-result JSON cache",
    )
    p.add_argument(
        "--force-refresh",
        action="store_true",
        help="Ignore any existing disk cache and re-call the LLM",
    )
    p.add_argument(
        "--mode",
        choices=["batch", "sync"],
        default="batch",
        help=(
            "LLM call mode.  'batch' (default): Anthropic Batch API — "
            "5–30 min wait, bypasses per-minute rate limits, 50%% cheaper.  "
            "'sync': messages.create — fast but throttled by per-minute "
            "input-token cap on lower account tiers."
        ),
    )
    p.add_argument(
        "--poll-interval",
        type=int,
        default=30,
        help="Seconds between batch status checks (only used in batch mode)",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=30 * 60,
        help="Max seconds to wait for batch completion (only used in batch mode)",
    )
    args = p.parse_args()

    if not args.pdf.exists():
        print(f"ERROR: PDF not found: {args.pdf}", file=sys.stderr)
        return 1

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY is not set.\n"
            "  - If you have a .env file at the project root, ensure it "
            "contains: ANTHROPIC_API_KEY=sk-ant-api03-...\n"
            "  - Or export it in your shell: "
            "export ANTHROPIC_API_KEY=sk-ant-api03-...\n"
            "  - Then re-run.",
            file=sys.stderr,
        )
        return 1

    # ---- Materials list ----
    if args.materials == "db":
        try:
            materials = _materials_from_db()
        except Exception as exc:
            print(
                f"ERROR: failed to fetch materials from DB: {exc}\n"
                "Re-run with `--materials hardcoded` to use the script's "
                "built-in list.",
                file=sys.stderr,
            )
            return 1
    else:
        materials = list(_HARDCODED_MATERIALS)
    print(f"Materials: {len(materials)} canonical names")
    for m in materials:
        print(f"  {m}")

    # ---- PDF text ----
    pdf_text = _read_pdf_text(args.pdf)

    # ---- Cache path ----
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = args.cache_dir / f"mcs{args.year}_locator_result.json"

    # ---- Call the locator ----
    from app.services.ingestion.mcs_pdf_llm_locator import locate_commodity_chapters

    print(f"\nCalling locate_commodity_chapters() …")
    print(f"  cache_path={cache_path}")
    print(f"  force_refresh={args.force_refresh}")

    # Make it unambiguous up-front whether we're hitting the cache or the LLM
    cache_existed_before = cache_path.exists()
    cache_size = cache_path.stat().st_size if cache_existed_before else 0
    if cache_existed_before and not args.force_refresh:
        print(
            f"\n  ⚠️  CACHE HIT EXPECTED: {cache_path} exists ({cache_size} bytes). "
            f"The LLM will NOT be called — result will be loaded from disk.\n"
            f"      Pass --force-refresh to bypass and call the LLM anyway."
        )
    elif cache_existed_before and args.force_refresh:
        print(
            f"\n  Cache file exists ({cache_size} bytes) but --force-refresh "
            f"was passed.  The LLM WILL be called and the cache overwritten."
        )
    else:
        print(f"\n  No cache file at {cache_path}.  The LLM WILL be called.")

    if args.mode == "batch" and (not cache_existed_before or args.force_refresh):
        print(
            "  Submitting via Batch API.  Anthropic typically completes "
            "batches in 5–30 minutes; you'll see polling status updates "
            "every {poll}s until it's done.".format(poll=args.poll_interval)
        )

    import time as _time
    _t_start = _time.monotonic()

    result = locate_commodity_chapters(
        pdf_text=pdf_text,
        canonical_material_names=materials,
        cache_path=cache_path,
        force_refresh=args.force_refresh,
        use_batch=(args.mode == "batch"),
        poll_interval_seconds=args.poll_interval,
        timeout_seconds=args.timeout,
    )

    _t_elapsed = _time.monotonic() - _t_start
    print(f"\n  locate_commodity_chapters() returned in {_t_elapsed:.1f}s")
    if _t_elapsed < 5:
        print(
            "  ⚠️  Returned almost instantly — this run almost certainly "
            "loaded from disk cache, not the LLM."
        )

    # ---- Print summary ----
    print(f"\n=== Locator result ===")
    print(f"Chapters: {len(result.chapters)}")
    print(f"Skipped:  {len(result.skipped)}")
    if result.notes:
        print(f"Notes:    {result.notes}")

    print(f"\n--- Chapters ---")
    for ch in sorted(result.chapters, key=lambda c: c.canonical_material):
        sec_types = ", ".join(s.section_type for s in ch.sections) or "(none)"
        print(
            f"  {ch.canonical_material:25s}  pp.{ch.start_page:>3}-{ch.end_page:<3}  "
            f"L{ch.start_line:>5}-{ch.end_line:<5}  [{sec_types}]"
        )

    if result.skipped:
        print(f"\n--- Skipped ---")
        for s in result.skipped:
            print(f"  {s.pdf_heading:30s}  →  {s.reason}")

    # ---- Sanity checks ----
    print(f"\n=== Sanity checks ===")

    # 1. BAUXITE AND ALUMINA should be in skipped, not chapters
    bauxite_in_chapters = any(
        "BAUXITE" in c.pdf_heading.upper() for c in result.chapters
    )
    bauxite_skipped = any(
        "BAUXITE" in s.pdf_heading.upper() for s in result.skipped
    )
    print(f"  BAUXITE not in chapters:   {'PASS' if not bauxite_in_chapters else 'FAIL'}")
    print(f"  BAUXITE in skipped:        {'PASS' if bauxite_skipped else 'FAIL'}")

    # 2. The three previously-broken commodities should be present
    expected_present = {"Aluminum", "Iron Ore", "Platinum-Group Metals"}
    chapter_materials = {c.canonical_material for c in result.chapters}
    missing = expected_present - chapter_materials
    if not missing:
        print(f"  Aluminum/Iron Ore/PGM all located: PASS")
    else:
        print(f"  Aluminum/Iron Ore/PGM all located: FAIL (missing: {sorted(missing)})")

    # 3. No chapter should span more than 5% of the PDF.  The current LLM
    #    workflow asks for chapter bounds only (sub-sections are handled by
    #    the deterministic regex extractors downstream), so we don't check
    #    section count anymore.  We DO check that each chapter's line range
    #    is sane: an MCS chapter is 1-4 pages = ~50-300 lines; anything
    #    >5% of the PDF is almost certainly a resolver bug (chapter near a
    #    layout-irregular region extending to EOF).
    pdf_total_lines = len(pdf_text.splitlines())
    max_allowed = int(pdf_total_lines * 0.05)
    oversized = [
        (c.canonical_material, c.end_line - c.start_line)
        for c in result.chapters
        if (c.end_line - c.start_line) > max_allowed
    ]
    if not oversized:
        print(f"  No oversized chapters (>{max_allowed} lines):  PASS")
    else:
        print(f"  No oversized chapters (>{max_allowed} lines):  FAIL")
        for name, n in oversized:
            print(f"    {name}: {n} lines")

    print(f"\nResult cached to: {cache_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
