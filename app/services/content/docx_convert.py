"""Shared docx → Markdown conversion for insight posts.

Single source of truth used by both:
  * ``scripts/ingest_insight_docx.py`` (CLI, partner-assisted ingestion)
  * ``POST /api/v1/intelligence/posts/upload-docx`` (admin UI upload)

Conversion pipeline (design decided 2026-07-08):
  docx → mammoth (HTML, images extracted through app.utils.storage under
  ``insights/{slug}/img_NN.ext``) → markdownify (Markdown) → ``[[chart: name]]``
  placeholder paragraphs become unconfigured ```chart fenced blocks that the
  admin editor's ChartConfigForm binds type/data to.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from typing import BinaryIO

import mammoth
from markdownify import markdownify as html_to_md

from app.utils.storage import get_local_storage

WORDS_PER_MINUTE = 220
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# [[chart: name]] — tolerate the backslash-escaped brackets markdownify emits
_PLACEHOLDER_RE = re.compile(r"\\?\[\\?\[chart:\s*([^\]\\]+)\\?\]\\?\]")


@dataclass
class ConversionResult:
    markdown: str
    image_paths: list[str] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    title: str | None = None
    read_time_minutes: int | None = None


def _placeholder_to_chart_block(match: re.Match) -> str:
    name = match.group(1).strip().lower().replace(" ", "-")
    spec = json.dumps({"name": name, "type": "unconfigured"})
    return f"\n```chart\n{spec}\n```\n"


def estimate_read_minutes(markdown: str) -> int:
    words = len(re.findall(r"\w+", markdown))
    return max(1, math.ceil(words / WORDS_PER_MINUTE))


def first_heading(markdown: str) -> str | None:
    """First heading of ANY level — partner docs don't reliably use Heading 1."""
    m = re.search(r"^#{1,4}\s+(.+)$", markdown, re.M)
    return m.group(1).strip() if m else None


def convert_docx_stream(fileobj: BinaryIO, slug: str) -> ConversionResult:
    """Convert an open .docx stream; images persist via the storage backend."""
    if not SLUG_RE.match(slug):
        raise ValueError(f"slug must be kebab-case: {slug!r}")

    storage = get_local_storage()
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
        return {"src": f"/content-assets/{stored}"}

    result = mammoth.convert_to_html(
        fileobj, convert_image=mammoth.images.img_element(store_image)
    )
    messages = [f"{m.type}: {m.message}" for m in result.messages]

    md = html_to_md(result.value, heading_style="ATX", bullets="-")
    md = re.sub(r"\n{3,}", "\n\n", md).strip() + "\n"
    md = _PLACEHOLDER_RE.sub(_placeholder_to_chart_block, md)

    return ConversionResult(
        markdown=md,
        image_paths=image_paths,
        messages=messages,
        title=first_heading(md),
        read_time_minutes=estimate_read_minutes(md),
    )
