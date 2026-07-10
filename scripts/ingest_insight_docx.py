"""Ingest a partner-authored .docx into an InsightPost draft.

Pipeline (designed 2026-07-08, see intelligence-hub content plan):

    partner .docx
      → mammoth (docx → clean HTML; images extracted via handler)
      → markdownify (HTML → Markdown)
      → images written through app.utils.storage (local now, R2 later)
      → InsightPost upserted as status='draft' (never auto-publish)

Charts are NOT authored in Word. The article renderer treats fenced
``chart`` code blocks in the Markdown body as declarative chart specs:

    ```chart
    {"type": "bar", "title": "DRC share of refined cobalt",
     "data": [{"label": "DRC", "value": 76}, {"label": "Other", "value": 24}],
     "source": "CMOC AR 2025"}
    ```

The frontend maps these to Recharts components (v1); individual specs can
be re-pointed at a d3 renderer later without touching published content.
A spec may use {"dataRef": "scores/latest?material=Cobalt"} instead of
inline data to pull live platform data. Partner or analyst pastes chart
blocks into the Markdown after conversion — Word charts/images embedded
in the docx come through as static images, which is also acceptable.

Usage:
    python scripts/ingest_insight_docx.py path/to/article.docx \
        --slug drc-cobalt-quota-2026 \
        --content-type analysis \
        --title "Optional override title" \
        --pillar geopolitical_trade \
        --materials Cobalt,Copper --geographies CD,CN \
        [--author "Partner Name"] [--dry-run]

Dependencies: pip install mammoth markdownify  (add to requirements.txt)

Notes:
  * status is always 'draft'; publishing is a separate, deliberate step.
  * Re-running with the same --slug updates the existing draft (upsert);
    it refuses to overwrite a post whose status is 'published'.
  * PDF-type Reports: don't use this script — upload the PDF to storage
    and set pdf_url on the row; the landing-page summary can still be
    authored here with --content-type report.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

try:
    import mammoth
except ImportError:  # pragma: no cover
    sys.exit("mammoth not installed — pip install mammoth markdownify")
try:
    from markdownify import markdownify as html_to_md
except ImportError:  # pragma: no cover
    sys.exit("markdownify not installed — pip install markdownify")

# repo imports (script is run from repo root)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.utils.storage import get_local_storage  # noqa: E402

WORDS_PER_MINUTE = 220
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
VALID_CONTENT_TYPES = {"analysis", "signal", "report", "news"}
VALID_PILLARS = {
    "material_concentration", "geopolitical_trade",
    "regulatory_compliance", "operational", "financial_pressure",
}


def convert_docx(docx_path: Path, slug: str, storage) -> tuple[str, list[str]]:
    """Return (markdown_body, image_storage_paths)."""
    image_paths: list[str] = []
    counter = {"n": 0}

    def store_image(image) -> dict:
        counter["n"] += 1
        ext = (image.content_type or "image/png").split("/")[-1]
        ext = {"jpeg": "jpg", "svg+xml": "svg"}.get(ext, ext)
        key = f"insights/{slug}/img_{counter['n']:02d}.{ext}"
        with image.open() as f:
            stored = storage.write_bytes(key, f.read())
        image_paths.append(stored)
        # Rendered URL: the API serves storage paths under /content-assets/
        # (swap prefix for the R2 public bucket URL at cutover).
        return {"src": f"/content-assets/{stored}"}

    with open(docx_path, "rb") as f:
        result = mammoth.convert_to_html(
            f, convert_image=mammoth.images.img_element(store_image)
        )
    for msg in result.messages:
        print(f"  mammoth: {msg.type}: {msg.message}", file=sys.stderr)

    md = html_to_md(result.value, heading_style="ATX", bullets="-")
    # normalise excess blank lines
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    # Partner chart placeholders (decided 2026-07-08): a paragraph like
    #   [[chart: cobalt-quota]]
    # becomes an empty chart block at that position; the admin editor
    # binds type/data/config to it by name. Escaped variants from Word
    # conversion (\[\[...\]\]) are handled too.
    def _placeholder(m: re.Match) -> str:
        name = m.group(1).strip().lower().replace(" ", "-")
        return (
            "\n```chart\n"
            + json.dumps({"name": name, "type": "unconfigured"})
            + "\n```\n"
        )
    md = re.sub(r"\\?\[\\?\[chart:\s*([^\]\\]+)\\?\]\\?\]", _placeholder, md)
    return md, image_paths


def estimate_read_minutes(markdown: str) -> int:
    words = len(re.findall(r"\w+", markdown))
    return max(1, math.ceil(words / WORDS_PER_MINUTE))


def first_heading(markdown: str) -> str | None:
    m = re.search(r"^#\s+(.+)$", markdown, re.M)
    return m.group(1).strip() if m else None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("docx", type=Path)
    ap.add_argument("--slug", required=True)
    ap.add_argument("--content-type", required=True, choices=sorted(VALID_CONTENT_TYPES))
    ap.add_argument("--title", default=None, help="Defaults to first H1 in the doc")
    ap.add_argument("--summary", default=None)
    ap.add_argument("--pillar", default=None, choices=sorted(VALID_PILLARS))
    ap.add_argument("--materials", default=None, help="Comma-separated canonical names")
    ap.add_argument("--geographies", default=None, help="Comma-separated ISO2 codes")
    ap.add_argument("--author", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="Convert and print; write images but no DB row")
    args = ap.parse_args()

    if not _SLUG_RE.match(args.slug):
        sys.exit(f"--slug must be kebab-case: {args.slug!r}")
    if not args.docx.exists():
        sys.exit(f"file not found: {args.docx}")

    storage = get_local_storage()
    body_md, images = convert_docx(args.docx, args.slug, storage)
    title = args.title or first_heading(body_md) or args.docx.stem
    read_min = estimate_read_minutes(body_md) if args.content_type in ("analysis", "report") else None

    print(f"converted: {len(body_md)} chars markdown, {len(images)} image(s) → storage")
    for p in images:
        print(f"  image: {p}")
    print(f"title: {title!r} | read_time: {read_min} | type: {args.content_type}")

    if args.dry_run:
        print("--- MARKDOWN PREVIEW (first 2000 chars) ---")
        print(body_md[:2000])
        return

    # DB upsert — deferred import so --dry-run works without DB env
    from sqlalchemy import select
    from app.db.session import get_session_factory
    from app.models.intelligence import InsightPost

    with get_session_factory()() as session:
        existing = session.execute(
            select(InsightPost).where(InsightPost.slug == args.slug)
        ).scalar_one_or_none()
        if existing is not None and existing.status == "published":
            sys.exit(
                f"refusing to overwrite PUBLISHED post '{args.slug}' — "
                "archive it or choose a new slug"
            )
        post = existing or InsightPost(slug=args.slug)
        post.title = title
        post.content_type = args.content_type
        post.pillar = args.pillar
        post.materials = [m.strip() for m in args.materials.split(",")] if args.materials else None
        post.geographies = [g.strip().upper() for g in args.geographies.split(",")] if args.geographies else None
        post.summary = args.summary
        post.body = body_md
        post.read_time_minutes = read_min
        post.author = args.author
        post.status = "draft"
        session.add(post)
        session.commit()
        print(f"InsightPost draft upserted: id={post.id} slug={post.slug}")


if __name__ == "__main__":
    main()
