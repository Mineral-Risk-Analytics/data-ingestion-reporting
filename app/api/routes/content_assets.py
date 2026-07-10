"""Serve insight-post images (and future content assets) over HTTP.

The docx conversion pipeline writes extracted images through the storage
backend (local filesystem now, R2 later) and embeds relative URLs of the
form ``/content-assets/raw/insights/{slug}/img_01.png`` in the Markdown
body. This route serves those bytes in development / pre-R2 production.

At R2 cutover this route becomes unnecessary: point the conversion's URL
prefix at the R2 public bucket instead and retire this module.

Mounted WITHOUT the /api/v1 prefix because the URLs live inside published
article bodies — they must stay short-lived-infrastructure-agnostic.
The frontend proxies /content-assets/* here via a Next.js rewrite, so the
relative URLs work identically in the admin preview and the public hub.

Security: read-only; path is normalised and must resolve inside the
storage root; only a small image/PDF mime whitelist is served.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from app.utils.storage import get_local_storage

router = APIRouter(tags=["content-assets"])

_ALLOWED_MIME = {
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
    "image/svg+xml",
    "application/pdf",
}


@router.get("/content-assets/{asset_path:path}")
def get_content_asset(asset_path: str) -> Response:
    storage = get_local_storage()
    root = storage.root

    # Normalise and refuse anything escaping the storage root.
    candidate = (root / asset_path).resolve()
    if root.resolve() not in candidate.parents and candidate != root.resolve():
        raise HTTPException(status_code=404, detail="not found")
    if not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")

    mime, _ = mimetypes.guess_type(candidate.name)
    if mime not in _ALLOWED_MIME:
        raise HTTPException(status_code=404, detail="not found")

    return Response(
        content=candidate.read_bytes(),
        media_type=mime,
        headers={
            # Content is immutable once written (numbered per upload) —
            # long cache is safe and keeps the hub snappy.
            "Cache-Control": "public, max-age=86400",
        },
    )
