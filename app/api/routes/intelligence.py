"""Intelligence Hub routes — Phase 4 backend for the public content site.

Two access tiers:

  * **Public (no auth)** — power the public Mineral Risk Analytics site:
      - ``GET  /intelligence/posts``                — paginated published feed
      - ``GET  /intelligence/posts/{slug}``         — single published post
      - ``GET  /intelligence/risk-summary``         — sidebar risk bars

  * **Admin (auth required)** — power the partner's authoring UI:
      - ``GET  /intelligence/posts/drafts``         — all non-published posts
      - ``POST /intelligence/posts``                — create a draft
      - ``PATCH /intelligence/posts/{post_id}``     — edit fields (no status)
      - ``POST /intelligence/posts/{post_id}/publish``
      - ``POST /intelligence/posts/{post_id}/unpublish``
      - ``POST /intelligence/posts/{post_id}/archive``

Status lifecycle is intentionally exposed as explicit verbs (publish /
unpublish / archive) rather than a free-form ``status`` field on PATCH so
the partner can't accidentally bypass the ``published_at`` stamping.

Route ordering note
-------------------
``/posts/drafts`` MUST be registered before ``/posts/{slug}`` because the
slug param is a string and would otherwise greedy-match the literal "drafts".
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

import re as _re

from app.api.deps import get_current_user, get_db, require_admin
from app.core.config import get_settings
from app.models.intelligence import InsightPost
from app.services.content.docx_convert import convert_docx_stream
from app.api.routes.intelligence_entities import _band_out
from app.models.scoring import MaterialGlobalRiskScore
from app.models.supply import Material
from app.schemas.common import PaginatedResponse
from app.models.company import Company
from app.models.country import Country
from app.models.regulatory import Regulation
from app.schemas.intelligence import (
    InsightPostCreate,
    InsightPostDetail,
    InsightPostListItem,
    InsightPostUpdate,
    MaterialRiskBar,
    EntityLinkOut,
    PdfUploadUrlIn,
    PdfUploadUrlOut,
    RiskSummary,
    FacetSuggestResponse,
    FacetSuggestion,
    TagClassifyIn,
    TagClassifyOut,
    TagSuggestResponse,
    TagSuggestion,
)

router = APIRouter(prefix="/intelligence", tags=["intelligence"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_post_or_404(db: Session, post_id: int) -> InsightPost:
    post = db.get(InsightPost, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


def _array_contains(column, value: str):
    """Filter rows whose ``column`` array contains ``value``.

    Compiles to PG's ``column @> ARRAY[value]`` for native ``ARRAY`` columns
    and to a JSON containment for SQLite-under-test (where ``ARRAY`` is
    rendered as ``JSON`` by the conftest shim — see ``tests/conftest.py``).
    Mirrors the pattern used elsewhere in the codebase for JSONB array
    membership (e.g. ``evidence_query.py`` against ``risk_categories_json``).
    """
    return column.contains([value])


# ===========================================================================
# Public routes — no auth
# ===========================================================================


# ---------------------------------------------------------------------------
# GET /intelligence/posts  (must be defined BEFORE /posts/{slug})
# ---------------------------------------------------------------------------


@router.get("/posts", response_model=PaginatedResponse[InsightPostListItem])
def list_published_posts(
    content_type: Optional[str] = Query(None, description="analysis | signal | report | news"),
    pillar: Optional[str] = Query(None),
    material: Optional[str] = Query(
        None, description="Match where material is in the post's materials array"
    ),
    geography: Optional[str] = Query(
        None, description="Match where geography (ISO2) is in the post's geographies array"
    ),
    tag: Optional[str] = Query(
        None, description="Match where tag is in the post's free-form tags array"
    ),
    page: int = Query(1, ge=1),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> PaginatedResponse[InsightPostListItem]:
    """Public feed of published intelligence posts, ordered newest first."""
    q = select(InsightPost).where(InsightPost.status == "published")

    if content_type is not None:
        q = q.where(InsightPost.content_type == content_type)
    if pillar is not None:
        q = q.where(InsightPost.pillar == pillar)
    if material is not None:
        q = q.where(_array_contains(InsightPost.materials, material))
    if geography is not None:
        q = q.where(_array_contains(InsightPost.geographies, geography.upper()))
    if tag:
        q = q.where(_array_contains(InsightPost.tags, tag))

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(
        q.order_by(
            InsightPost.pinned.desc(),  # pinned posts surface first (051)
            InsightPost.published_at.desc().nullslast(),
            InsightPost.id.desc(),
        )
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    return PaginatedResponse(
        data=[InsightPostListItem.model_validate(r) for r in rows],
        total=total or 0,
        page=page,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# GET /intelligence/posts/drafts  (admin — registered before /posts/{slug})
# ---------------------------------------------------------------------------


@router.get(
    "/posts/drafts",
    response_model=PaginatedResponse[InsightPostListItem],
)
def list_draft_posts(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PaginatedResponse[InsightPostListItem]:
    """Admin inbox: every post NOT in ``published`` state (draft + archived)."""
    q = select(InsightPost).where(InsightPost.status != "published")

    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(
        q.order_by(InsightPost.updated_at.desc(), InsightPost.id.desc())
        .offset((page - 1) * limit)
        .limit(limit)
    ).all()

    return PaginatedResponse(
        data=[InsightPostListItem.model_validate(r) for r in rows],
        total=total or 0,
        page=page,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# GET /intelligence/risk-summary
# ---------------------------------------------------------------------------


@router.get("/risk-summary", response_model=RiskSummary)
def get_risk_summary(db: Session = Depends(get_db)) -> RiskSummary:
    """Public sidebar feed — latest L2 global rollup per material.

    2026-07-21 rewrite (was: highest-scoring single GEOGRAPHY per material
    from L1 rows — predated the global rollup and publicly disagreed with
    the platform materials page). Now reads ``material_global_risk_scores``
    (latest row per material) so the public hub and the authenticated
    platform report the same number, banded by the shared ``bands.py``
    cuts via ``_band_out``.

    Insufficient-data gate: rows whose ``material_concentration_score`` is
    NULL or 0 are EXCLUDED. Global concentration is a MAX over scored
    stages, so 0 means "no scored stage", not "perfectly diversified" —
    banding those would read false-low in public (e.g. Germanium).
    Mirrors ``MaterialListItem.concentration_scored`` on the platform side.
    """
    ranked = (
        select(
            MaterialGlobalRiskScore.material_id,
            MaterialGlobalRiskScore.overall_risk_score,
            MaterialGlobalRiskScore.material_concentration_score,
            MaterialGlobalRiskScore.as_of_date,
            func.row_number()
            .over(
                partition_by=MaterialGlobalRiskScore.material_id,
                order_by=MaterialGlobalRiskScore.as_of_date.desc(),
            )
            .label("rn"),
        )
        .subquery()
    )
    rows = db.execute(
        select(
            ranked.c.material_id,
            ranked.c.overall_risk_score,
            ranked.c.material_concentration_score,
            ranked.c.as_of_date,
            Material.canonical_name,
        )
        .join(Material, Material.id == ranked.c.material_id)
        .where(ranked.c.rn == 1)
    ).all()

    bars: list[MaterialRiskBar] = []
    as_of = None
    for material_id, overall, concentration, row_date, name in rows:
        if overall is None:
            continue
        if concentration is None or concentration <= 0:
            continue  # insufficient-data gate
        band = _band_out(overall)
        if band is None:
            continue
        bars.append(
            MaterialRiskBar(
                material_id=material_id,
                material_name=name,
                band=band,
            )
        )
        if as_of is None or (row_date is not None and row_date > as_of):
            as_of = row_date

    bars.sort(key=lambda b: (-(b.band.score or 0.0), b.material_name))
    return RiskSummary(as_of_date=as_of, materials=bars)


# ---------------------------------------------------------------------------
# GET /intelligence/posts/{slug}  (public — must come AFTER /posts/drafts)
# ---------------------------------------------------------------------------


def _resolve_entity_links(db: Session, tags: list[str]) -> list[EntityLinkOut]:
    """Map a post's tags to outbound links for publicly-visible entities.

    Company tag = canonical_name but the URL needs the SLUG, so we resolve;
    regulation tag IS the regulation_key (== URL param). Only published /
    verified entities are linked (mirrors the public entity routes' gates)
    so an article never links to a page that would 404. Preserves tag
    order; a tag matching neither is just a plain topic tag (omitted).
    """
    if not tags:
        return []
    companies = {
        name: slug
        for name, slug in db.execute(
            select(Company.canonical_name, Company.slug).where(
                Company.canonical_name.in_(tags),
                Company.is_published.is_(True),
            )
        )
    }
    regulations = {
        key
        for (key,) in db.execute(
            select(Regulation.regulation_key).where(
                Regulation.regulation_key.in_(tags),
                Regulation.verified.is_(True),
            )
        )
    }
    out: list[EntityLinkOut] = []
    for t in tags:
        if t in companies:
            _slug = companies[t] or ""
            out.append(EntityLinkOut(
                kind="company", label=t,
                url=f"/intelligence/companies/{_slug}",
            ))
        elif t in regulations:
            out.append(EntityLinkOut(
                kind="regulation", label=t,
                url=f"/intelligence/regulations/{t}",
            ))
    return out


@router.get("/posts/{slug}", response_model=InsightPostDetail)
def get_published_post(slug: str, db: Session = Depends(get_db)) -> InsightPostDetail:
    """Single post by slug, restricted to published state."""
    post = db.scalar(
        select(InsightPost).where(
            InsightPost.slug == slug,
            InsightPost.status == "published",
        )
    )
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    detail = InsightPostDetail.model_validate(post)
    detail.entity_links = _resolve_entity_links(db, post.tags or [])
    return detail


# ===========================================================================
# Admin routes — auth required
# ===========================================================================


# ---------------------------------------------------------------------------
# POST /intelligence/posts
# ---------------------------------------------------------------------------


@router.post(
    "/posts",
    response_model=InsightPostDetail,
    status_code=status.HTTP_201_CREATED,
)
def create_post(
    body: InsightPostCreate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InsightPostDetail:
    """Create a new ``InsightPost`` in ``draft`` state."""
    post = InsightPost(
        slug=body.slug,
        title=body.title,
        content_type=body.content_type,
        pillar=body.pillar,
        materials=body.materials,
        geographies=body.geographies,
        summary=body.summary,
        body=body.body,
        pdf_url=body.pdf_url,
        read_time_minutes=body.read_time_minutes,
        author=body.author,
        metadata_json=body.metadata_json,
        published_at=body.published_at,
        status="draft",
    )
    db.add(post)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"slug '{body.slug}' is already in use",
        ) from exc
    db.refresh(post)
    return InsightPostDetail.model_validate(post)


# ---------------------------------------------------------------------------
# PATCH /intelligence/posts/{post_id}
# ---------------------------------------------------------------------------


@router.patch("/posts/{post_id}", response_model=InsightPostDetail)
def update_post(
    post_id: int,
    body: InsightPostUpdate,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InsightPostDetail:
    """Update editable fields. Status transitions go through dedicated verbs."""
    post = _get_post_or_404(db, post_id)

    updates = body.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(post, field, value)

    try:
        db.commit()
    except IntegrityError as exc:
        # slug is unique — surfaces when a PATCH renames onto a taken slug.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"slug '{body.slug}' is already in use",
        ) from exc
    db.refresh(post)
    return InsightPostDetail.model_validate(post)


# ---------------------------------------------------------------------------
# POST /intelligence/posts/{post_id}/publish
# ---------------------------------------------------------------------------


@router.post("/posts/{post_id}/publish", response_model=InsightPostDetail)
def publish_post(
    post_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InsightPostDetail:
    """Transition a post to ``published`` and stamp ``published_at``."""
    post = _get_post_or_404(db, post_id)
    if post.status == "published":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Post is already published",
        )
    post.status = "published"
    if post.published_at is None:
        post.published_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(post)
    return InsightPostDetail.model_validate(post)


# ---------------------------------------------------------------------------
# POST /intelligence/posts/{post_id}/unpublish
# ---------------------------------------------------------------------------


@router.post("/posts/{post_id}/unpublish", response_model=InsightPostDetail)
def unpublish_post(
    post_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InsightPostDetail:
    """Pull a post back to ``draft`` and clear ``published_at``."""
    post = _get_post_or_404(db, post_id)
    post.status = "draft"
    post.published_at = None
    db.commit()
    db.refresh(post)
    return InsightPostDetail.model_validate(post)


# ---------------------------------------------------------------------------
# POST /intelligence/posts/{post_id}/archive
# ---------------------------------------------------------------------------


@router.post("/posts/{post_id}/archive", response_model=InsightPostDetail)
def archive_post(
    post_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InsightPostDetail:
    """Soft-delete a post. Row is preserved so old URLs can 301-redirect."""
    post = _get_post_or_404(db, post_id)
    post.status = "archived"
    db.commit()
    db.refresh(post)
    return InsightPostDetail.model_validate(post)


# ---------------------------------------------------------------------------
# POST /intelligence/posts/upload-docx  (admin UI, added 2026-07-08)
# ---------------------------------------------------------------------------


@router.post(
    "/posts/upload-docx",
    response_model=InsightPostDetail,
    status_code=status.HTTP_201_CREATED,
)
def upload_docx(
    file: UploadFile = File(...),
    slug: str = Form(...),
    content_type: str = Form(...),
    title: Optional[str] = Form(None),
    author: Optional[str] = Form(None),
    published_at: Optional[str] = Form(
        None, description="Article (written) date, ISO format, e.g. 2026-07-15"
    ),
    _user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
) -> InsightPostDetail:
    """Convert a partner-authored .docx into a draft InsightPost.

    Runs the shared mammoth→markdownify pipeline (images extracted to
    storage under ``insights/{slug}/``; ``[[chart: name]]`` placeholders
    become unconfigured chart blocks for the editor's ChartConfigForm).
    Upserts by slug; refuses to overwrite a published post.
    """
    if content_type not in {"analysis", "signal", "report", "news"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid content_type '{content_type}'",
        )
    try:
        result = convert_docx_stream(file.file, slug)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc

    existing = db.execute(
        select(InsightPost).where(InsightPost.slug == slug)
    ).scalar_one_or_none()
    if existing is not None and existing.status == "published":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"post '{slug}' is published — archive it or use a new slug",
        )

    post = existing or InsightPost(slug=slug)
    # Filename fallback: strip extension + prettify ("first-article.docx"
    # -> "First article") — never surface raw filenames as public titles.
    fallback = Path(file.filename or slug).stem.replace("-", " ").replace("_", " ").strip()
    fallback = fallback[:1].upper() + fallback[1:] if fallback else slug
    post.title = title or result.title or fallback
    post.content_type = content_type
    post.body = result.markdown
    post.read_time_minutes = (
        result.read_time_minutes if content_type in ("analysis", "report") else None
    )
    if author:
        post.author = author
    if published_at:
        # Article (written) date — parsed leniently: date-only strings
        # become midnight UTC. Invalid input -> 422, not a silent skip.
        try:
            _parsed = datetime.fromisoformat(published_at)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"published_at is not an ISO date: '{published_at}'",
            ) from exc
        if _parsed.tzinfo is None:
            _parsed = _parsed.replace(tzinfo=timezone.utc)
        post.published_at = _parsed
    post.status = "draft"
    db.add(post)
    db.commit()
    db.refresh(post)
    return InsightPostDetail.model_validate(post)


@router.get("/metadata/materials", response_model=FacetSuggestResponse)
def suggest_materials(
    q: str = Query(min_length=1, max_length=128),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FacetSuggestResponse:
    """Materials autocomplete for the editor (canonical name or symbol).
    value = canonical_name — exactly what materials[] stores."""
    like = f"%{q}%"
    rows = db.execute(
        select(Material.canonical_name, Material.symbol_or_code)
        .where(
            Material.canonical_name.ilike(like)
            | Material.symbol_or_code.ilike(like)
        )
        .order_by(func.length(Material.canonical_name))
        .limit(10)
    ).all()
    return FacetSuggestResponse(
        suggestions=[
            FacetSuggestion(value=name, label=name, hint=sym)
            for name, sym in rows
        ]
    )


@router.get("/metadata/geographies", response_model=FacetSuggestResponse)
def suggest_geographies(
    q: str = Query(min_length=1, max_length=128),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FacetSuggestResponse:
    """Country autocomplete for the editor (name or ISO2).
    value = ISO2 — exactly what geographies[] stores."""
    like = f"%{q}%"
    rows = db.execute(
        select(Country.iso2, Country.name)
        .where(Country.name.ilike(like) | Country.iso2.ilike(like))
        .order_by(func.length(Country.name))
        .limit(10)
    ).all()
    return FacetSuggestResponse(
        suggestions=[
            FacetSuggestion(value=iso2, label=f"{iso2} \u2014 {name}", hint=None)
            for iso2, name in rows
        ]
    )


@router.get("/tags/suggest", response_model=TagSuggestResponse)
def suggest_tags(
    q: str = Query(min_length=2, max_length=128),
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TagSuggestResponse:
    """Entity-tag autocomplete (admin editor). Merges companies (by
    canonical/legal name) and regulations (by key/title), 8 each. Includes
    unpublished companies deliberately — tagging is harmless pre-publish
    and the link lights up when the entity page goes live."""
    like = f"%{q}%"
    companies = db.execute(
        select(Company.canonical_name, Company.legal_name)
        .where(
            Company.canonical_name.ilike(like) | Company.legal_name.ilike(like)
        )
        .order_by(func.length(Company.canonical_name))
        .limit(8)
    ).all()
    regulations = db.execute(
        select(Regulation.regulation_key, Regulation.title)
        .where(
            Regulation.regulation_key.ilike(like) | Regulation.title.ilike(like)
        )
        .order_by(func.length(Regulation.regulation_key))
        .limit(8)
    ).all()
    return TagSuggestResponse(
        suggestions=[
            TagSuggestion(label=name, kind="company", hint=legal)
            for name, legal in companies
        ]
        + [
            TagSuggestion(label=key, kind="regulation", hint=title)
            for key, title in regulations
        ]
    )


@router.post("/tags/classify", response_model=TagClassifyOut)
def classify_tags(
    body: TagClassifyIn,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> TagClassifyOut:
    """Which of these tags are entity tags (exact-match linked_posts
    contract)? Editor calls this once per post load so pre-existing tags
    render with the right chip style."""
    tags = [t for t in body.tags if t]
    out: dict[str, Optional[str]] = {t: None for t in tags}
    if tags:
        for (name,) in db.execute(
            select(Company.canonical_name).where(Company.canonical_name.in_(tags))
        ):
            out[name] = "company"
        for (key,) in db.execute(
            select(Regulation.regulation_key).where(Regulation.regulation_key.in_(tags))
        ):
            out[key] = "regulation"
    return TagClassifyOut(classifications=out)


@router.post(
    "/posts/{post_id}/pdf-upload-url",
    response_model=PdfUploadUrlOut,
)
def create_pdf_upload_url(
    post_id: int,
    body: PdfUploadUrlIn,
    _user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
) -> PdfUploadUrlOut:
    """Presigned R2 PUT for a report's PDF (admin content v1, 2026-07-22).

    Browser flow: call this → PUT the file to ``upload_url`` (Content-Type
    application/pdf; the R2 bucket needs a CORS rule allowing PUT from the
    app origin) → save ``public_url`` into the post's ``pdf_url``.

    503 when the R2_* env vars are unset — deliberate, so a misconfigured
    deploy fails loudly instead of minting URLs that can never resolve.
    """
    post = _get_post_or_404(db, post_id)
    if post.content_type != "report":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PDF uploads are for report-type posts",
        )

    settings = get_settings()
    if not all(
        (
            settings.r2_account_id,
            settings.r2_access_key_id,
            settings.r2_secret_access_key,
            settings.r2_bucket,
            settings.r2_public_base_url,
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="R2 storage is not configured (set the R2_* env vars)",
        )

    # Sanitize: basename only, safe charset, force .pdf.
    name = _re.sub(r"[^A-Za-z0-9._-]+", "-", body.filename.rsplit("/", 1)[-1]).strip("-.")
    if not name:
        name = "report"
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    key = f"insights/pdf/{post.slug}/{name}"

    import boto3  # lazy: only needed when R2 is actually used

    client = boto3.client(
        "s3",
        endpoint_url=f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=settings.r2_access_key_id,
        aws_secret_access_key=settings.r2_secret_access_key,
        region_name="auto",
    )
    expires = 900
    upload_url = client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.r2_bucket,
            "Key": key,
            "ContentType": "application/pdf",
        },
        ExpiresIn=expires,
    )
    return PdfUploadUrlOut(
        upload_url=upload_url,
        public_url=f"{settings.r2_public_base_url.rstrip('/')}/{key}",
        key=key,
        expires_in=expires,
    )


@router.get("/posts/by-id/{post_id}", response_model=InsightPostDetail)
def get_post_any_status(
    post_id: int,
    _user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> InsightPostDetail:
    """Fetch a post regardless of status — admin editor needs draft bodies.

    (``GET /posts/{slug}`` is the public route and only serves published.)
    """
    return InsightPostDetail.model_validate(_get_post_or_404(db, post_id))


# ---------------------------------------------------------------------------
# Pinning (editorial top-of-feed placement — Nicole 2026-07-08)
# ---------------------------------------------------------------------------


@router.post("/posts/{post_id}/pin", response_model=InsightPostDetail)
def pin_post(
    post_id: int,
    _user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
) -> InsightPostDetail:
    """Pin a post above the date-ordered feed.

    Reports are not pinnable — the Featured slot (latest published report)
    is their placement mechanism; two competing mechanisms would conflict.
    """
    post = _get_post_or_404(db, post_id)
    if post.content_type == "report":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="reports use the featured slot and cannot be pinned",
        )
    post.pinned = True
    db.commit()
    db.refresh(post)
    return InsightPostDetail.model_validate(post)


@router.post("/posts/{post_id}/unpin", response_model=InsightPostDetail)
def unpin_post(
    post_id: int,
    _user: dict = Depends(require_admin),
    db: Session = Depends(get_db),
) -> InsightPostDetail:
    """Return a post to its natural date position in the feed."""
    post = _get_post_or_404(db, post_id)
    post.pinned = False
    db.commit()
    db.refresh(post)
    return InsightPostDetail.model_validate(post)
