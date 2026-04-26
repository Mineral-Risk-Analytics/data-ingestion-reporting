# Foundation Phases 1–4: Market Intelligence Layer + Intelligence Hub Backend

## Context and goal

This prompt covers four sequential build phases that complete the market
intelligence foundation before any company-profile work begins.

The architecture has two distinct scoring layers:

1. **Market layer** (company-agnostic) — scores at material × geography
   intersection. Built. Needs API surface and scheduled jobs.
2. **Company overlay** (paid tier) — blends market scores with a customer's
   self-reported exposure profile. **Not in scope here — do not start this.**

The eventual public product is **Mineral Risk Analytics** — an intelligence
hub at mineralriskanalytics.com that displays market-level mineral supply
chain risk scores alongside manually authored analytical content. Phases 1–4
build the backend that page will consume. The frontend page comes after.

---

## Repo and tech stack

- **FastAPI** + **SQLAlchemy ORM** (mapped_column style, not legacy Column)
- **Pydantic v2** — use `model_validate`, not `from_orm`
- **Alembic** — migrations already applied through `012_insight_posts`
- **Neon Postgres** (psycopg driver)
- Auth via `app.api.deps.get_current_user` (Clerk JWT) — inject as
  `_user: dict = Depends(get_current_user)` on protected routes
- DB session via `db: Session = Depends(get_db)`
- All routes registered under `/api/v1/` prefix in the main app router

**Use existing files as templates:**
- `app/api/routes/chemistries.py` — route style, pagination, 404 helper
- `app/api/routes/materials.py` — `PaginatedResponse`, query filters
- `app/schemas/common.py` — `PaginatedResponse`, `VerifiedResponse`

---

## Files already written — do not recreate

| File | Status |
|------|--------|
| `app/services/scoring/market_aggregator.py` | Complete |
| `app/models/scoring.py` → `MaterialGeographyRiskScore` | Complete |
| `app/models/intelligence.py` → `InsightPost` | Complete |
| `alembic/versions/011_mat_geo_risk_scores.py` | Applied |
| `alembic/versions/012_insight_posts.py` | Applied |

---

## Phase 1 — Chemistry risk completeness (~2 days)

### 1a. Fix hardcoded `trade_volatility` in `chemistry_risk.py`

**File:** `app/services/scoring/chemistry_risk.py`

**Problem:** Line ~297 passes `trade_volatility=0.3` as a fixed neutral value
to `score_material_exposure()` for every material in every chemistry. Events
never affect chemistry scores — a UFLPA enforcement action against a Chinese
graphite supplier has zero effect on the LFP score today.

**Fix:** For each material in the active composition rows, pull its
`GEOPOLITICAL_TRADE` events via `get_events_for_material()` from
`evidence_query.py` and derive a per-material `trade_volatility` from the
average normalised event impact. The pattern for this calculation already
exists in `market_aggregator._avg_impact_normalised()` — use the same logic.

```python
# In score_chemistry(), inside the per-material loop, replace:
trade_volatility=0.3  # neutral default

# With:
mat_trade_events = get_events_for_material(
    session, material.id, RiskCategory.GEOPOLITICAL_TRADE, as_of_date
)
trade_volatility = _avg_impact_normalised(mat_trade_events, RiskCategory.GEOPOLITICAL_TRADE, as_of_date)
```

Import `get_events_for_material` from `app.services.scoring.evidence_query`
and add the `_avg_impact_normalised` helper (copy the one from
`market_aggregator.py` or move it to a shared `_scoring_utils.py` — your
call, just avoid circular imports).

Record the per-material `trade_volatility` values in `metadata_json` alongside
the existing `signal_sources` and `geo_coverage` fields.

### 1b. Add `GET /chemistries/{id}` detail route

**File:** `app/api/routes/chemistries.py`

The list endpoint (`GET /chemistries`) already embeds `latest_risk_score`.
What is missing is a detail endpoint for a single chemistry that includes:
- Full `BatteryChemistry` fields
- Latest `ChemistryRiskScore` with full `metadata_json`
- All active `BatteryChemistryMaterial` rows (with `Material.canonical_name`
  resolved) for the current date — so the UI can show the composition table

Create a `ChemistryDetailRead` Pydantic schema in `app/schemas/chemistries.py`
that extends `BatteryChemistryRead` with:
```python
latest_risk_score: Optional[ChemistryRiskScoreRead]
active_materials: list[ChemistryMaterialRead]  # intensity + material name
```

Route:
```
GET /chemistries/{chemistry_id}
```
Protected (requires auth). Returns 404 if chemistry not found.

### 1c. Add rescore trigger endpoints

**File:** `app/api/routes/chemistries.py`

Two endpoints:

```
POST /chemistries/rescore
```
Triggers `rescore_all_chemistries(db, date.today())` from
`app.services.scoring.chemistry_risk`. Returns list of
`{slug, composite_risk_score, score_confidence}` dicts. Protected (auth).

```
POST /chemistries/{chemistry_id}/rescore
```
Triggers `rescore_one_chemistry(db, chemistry_id, date.today())`. Returns the
new `ChemistryRiskScoreRead`. Returns 404 if chemistry not found. Protected.

### 1d. Add risk score history endpoint

**File:** `app/api/routes/chemistries.py`

```
GET /chemistries/{chemistry_id}/risk/history
```
Returns all `ChemistryRiskScore` rows for this chemistry, ordered by
`as_of_date` desc. Query param: `limit: int = 12` (default last 12 runs).
Return `list[ChemistryRiskScoreRead]`. Protected.

---

## Phase 2 — Market scores API surface (~2 days)

### Create `app/api/routes/market_scores.py`

New router file. Register it in the main app router.

All routes read from `material_geography_risk_scores` via
`MaterialGeographyRiskScore` ORM model (`app.models.scoring`). The scoring
function is `score_material_geography()` and
`score_all_active_materials()` from `app.services.scoring.market_aggregator`.

#### Schemas needed (create in `app/schemas/market_scores.py`)

```python
class MaterialGeographyScoreRead(BaseModel):
    id: int
    material_id: int
    geography_code: str
    as_of_date: date
    material_concentration_score: Optional[float]
    geopolitical_trade_score: Optional[float]
    regulatory_compliance_score: Optional[float]
    operational_score: Optional[float]
    financial_pressure_score: Optional[float]
    overall_risk_score: Optional[float]
    event_count: int
    scoring_version: str
    created_at: datetime
    # Omit rationale_json from list views — include only in detail view

class MaterialGeographyScoreDetail(MaterialGeographyScoreRead):
    rationale_json: Optional[dict]  # full rationale for detail view

class RescoredResult(BaseModel):
    scored: int
    as_of_date: date
    run_id: str
```

#### Routes

```
GET /materials/{material_id}/market-scores
```
Returns the **latest** `MaterialGeographyRiskScore` per geography for this
material (one row per geography, most recent `as_of_date`). Use a subquery or
`DISTINCT ON (geography_code)` ordered by `as_of_date DESC`. Returns
`list[MaterialGeographyScoreRead]`. Protected.

```
GET /materials/{material_id}/market-scores/{geography_code}
```
Returns the single latest score for this (material, geography) pair with full
`rationale_json`. Returns `MaterialGeographyScoreDetail`. 404 if no score
exists yet. Protected.

```
GET /market/scores
```
Browse/filter endpoint. Query params:
- `material_id: Optional[int]`
- `geography_code: Optional[str]`
- `min_overall: Optional[float]` — filter to scores >= this value
- `page: int = 1`, `limit: int = 50`

Returns `PaginatedResponse[MaterialGeographyScoreRead]` with latest score per
(material, geography) pair matching the filters. Protected.

```
POST /market/rescore
```
Triggers `score_all_active_materials(db, date.today())`. This can take
seconds to minutes depending on event volume — for now run synchronously and
return the result. Returns `RescoredResult`. Protected.

**Important:** `score_all_active_materials()` commits inside its own loop per
pair. Do NOT wrap it in an additional outer transaction.

---

## Phase 3 — Scheduled jobs and ingestion cleanup (~1 day)

### 3a. Wire Inngest scheduled functions

**File:** `app/tasks/scoring_jobs.py` (currently `app/tasks/__init__.py` is
a one-line placeholder)

The project uses **Inngest** for job orchestration (see architecture decisions
in `docs/overview.md`). Create two scheduled functions:

```python
# Weekly chemistry rescore — runs every Monday at 02:00 UTC
@inngest_client.create_function(
    fn_id="rescore-all-chemistries",
    trigger=inngest.TriggerCron(cron="0 2 * * MON"),
)
async def rescore_chemistries_job(ctx, step):
    ...

# Weekly market rescore — runs every Monday at 03:00 UTC (after chemistry)
@inngest_client.create_function(
    fn_id="rescore-market-scores",
    trigger=inngest.TriggerCron(cron="0 3 * * MON"),
)
async def rescore_market_scores_job(ctx, step):
    ...
```

Follow the Inngest FastAPI integration pattern. The Inngest client setup
should go in `app/tasks/__init__.py` or `app/core/inngest.py` — whichever
keeps the import graph clean.

Register the Inngest router with the FastAPI app (standard Inngest FastAPI
serve endpoint at `/api/inngest`).

### 3b. Add `rescore-market` CLI command

**File:** `app/cli.py`

`score_all_active_materials()` from `market_aggregator.py` currently has no
CLI entry point. Add one so the market layer can be triggered manually after
ingestion runs, without needing the API to be running.

```python
@app.command("rescore-market")
def rescore_market_cmd(
    as_of: Optional[str] = typer.Option(
        None,
        "--as-of",
        help="Point-in-time date for scoring (YYYY-MM-DD). Default: today.",
    ),
    material_id: Optional[int] = typer.Option(
        None,
        "--material-id",
        help="Score only this material (all geographies). Default: all active materials.",
    ),
) -> None:
    """Compute and persist material_geography_risk_scores rows.

    Runs the market-level (company-agnostic) scoring engine across all active
    materials and their associated geographies. Safe to re-run — results are
    upserted by (material_id, geography_code, as_of_date).

    Run this AFTER ingest-worldbank and ingest-comtrade so that commodity price
    and trade flow data are available to the scoring engine.

    \b
    Examples:
      bdi-ingest rescore-market
      bdi-ingest rescore-market --as-of 2025-01-01
      bdi-ingest rescore-market --material-id 3
    """
    import datetime

    from app.services.scoring.market_aggregator import (
        score_all_active_materials,
        score_material_geography,
    )
    from app.models.supply import Material

    as_of_date = datetime.date.fromisoformat(as_of) if as_of else datetime.date.today()
    s = _session()
    try:
        if material_id:
            mat = s.get(Material, material_id)
            if mat is None:
                typer.echo(f"No material with id={material_id}", err=True)
                raise typer.Exit(code=1)
            # score_all_active_materials accepts an optional material filter;
            # if that signature doesn't exist, call score_material_geography
            # per geography derived from mat.primary_producing_countries
            results = score_all_active_materials(s, as_of_date, material_ids=[material_id])
        else:
            results = score_all_active_materials(s, as_of_date)

        scored = len(results) if results else 0
        typer.echo(f"rescore-market complete: {scored} (material, geography) pairs scored for {as_of_date}")
    except Exception as exc:
        typer.echo(f"rescore-market failed: {exc}", err=True)
        raise typer.Exit(code=1)
    finally:
        s.close()
```

**Note on `score_all_active_materials` signature:** The function currently
takes `(session, as_of_date)`. If Cursor adds `material_ids` filtering in
Phase 2, extend the signature there. If not, simplify the CLI to always
score all materials and drop the `--material-id` flag for now — don't over-engineer.

**Intended usage order after ingestion:**
```
bdi-ingest ingest-worldbank --since-year 2015
bdi-ingest ingest-comtrade --years 2021,2022,2023
bdi-ingest rescore-market
```

---

### 3c. Stop ingestion writing to `risk_event_companies`

**What to change:** The ingestion pipeline currently links risk events to
companies by writing rows to the `risk_event_companies` junction table. Under
the new architecture the ingestion pipeline feeds the market intelligence
layer only — company-event relationships will be derived from configured
exposure profiles (Phase 5, not in scope here).

**Find the write sites:** Search for `RiskEventCompany` and `risk_event_companies`
in `app/services/` and `app/ingestion/`. Comment out or guard the insert
statements with a feature flag. Do NOT drop the table or its existing data.

**Suggested approach:**
```python
# In whichever ingestion service builds RiskEventCompany rows:
LINK_EVENTS_TO_COMPANIES = False  # set True to re-enable legacy behaviour

if LINK_EVENTS_TO_COMPANIES:
    db.add(RiskEventCompany(...))
```

Log a `structlog` WARNING when the flag is False and an event would have been
linked, so it is auditable.

---

## Phase 4 — Intelligence hub backend (~2 days)

The `InsightPost` model (`app/models/intelligence.py`) and migration
(`012_insight_posts`) are already written and applied.

### Create `app/api/routes/intelligence.py`

New router. Two access levels:

- **Public routes** — no `get_current_user` dependency. These feed the public
  intelligence hub page (no login required).
- **Admin routes** — require `get_current_user`. Used by the partner to
  publish content from a simple form.

#### Schemas (create `app/schemas/intelligence.py`)

```python
class InsightPostListItem(BaseModel):
    id: int
    slug: str
    title: str
    content_type: str          # analysis | signal | report | news
    pillar: Optional[str]
    materials: Optional[list[str]]
    geographies: Optional[list[str]]
    summary: Optional[str]
    read_time_minutes: Optional[int]
    author: Optional[str]
    published_at: Optional[datetime]
    pdf_url: Optional[str]     # non-null for report type

class InsightPostDetail(InsightPostListItem):
    body: Optional[str]        # full markdown — omitted from list view
    metadata_json: Optional[dict]

class InsightPostCreate(BaseModel):
    slug: str
    title: str
    content_type: str
    pillar: Optional[str] = None
    materials: Optional[list[str]] = None
    geographies: Optional[list[str]] = None
    summary: Optional[str] = None
    body: Optional[str] = None
    pdf_url: Optional[str] = None
    read_time_minutes: Optional[int] = None
    author: Optional[str] = None
    metadata_json: Optional[dict] = None

class InsightPostUpdate(BaseModel):
    # All fields optional for PATCH semantics
    title: Optional[str] = None
    pillar: Optional[str] = None
    materials: Optional[list[str]] = None
    geographies: Optional[list[str]] = None
    summary: Optional[str] = None
    body: Optional[str] = None
    pdf_url: Optional[str] = None
    read_time_minutes: Optional[int] = None
    author: Optional[str] = None
    metadata_json: Optional[dict] = None

class MaterialRiskBar(BaseModel):
    """For the sidebar risk indicator bars on the intelligence hub."""
    material_name: str
    overall_risk_score: Optional[float]
    top_geography: Optional[str]   # geography with the highest overall score
    top_geography_score: Optional[float]

class RiskSummary(BaseModel):
    as_of_date: Optional[date]
    materials: list[MaterialRiskBar]
```

#### Public routes (no auth)

```
GET /intelligence/posts
```
Query params:
- `content_type: Optional[str]` — filter to analysis | signal | report | news
- `pillar: Optional[str]`
- `material: Optional[str]` — filter where `material = ANY(materials)`
- `geography: Optional[str]` — filter where `geography = ANY(geographies)`
- `page: int = 1`, `limit: int = 20`

Returns `PaginatedResponse[InsightPostListItem]` for published posts only
(`status = 'published'`), ordered by `published_at DESC`.

```
GET /intelligence/posts/{slug}
```
Returns `InsightPostDetail` for published post with this slug. 404 if not
found or not published.

```
GET /intelligence/risk-summary
```
Returns `RiskSummary`. Reads the latest `MaterialGeographyRiskScore` per
material and picks the highest-scoring geography per material. Used to
populate the sidebar risk indicator bars on the public page.

Only include materials that have at least one score row. Order by
`overall_risk_score DESC`. No auth required.

#### Admin routes (auth required)

```
POST /intelligence/posts
```
Creates a new `InsightPost` with `status='draft'`. Body: `InsightPostCreate`.
Returns `InsightPostDetail` with `status=201`.

```
PATCH /intelligence/posts/{post_id}
```
Updates editable fields. Body: `InsightPostUpdate`. Does not change `status`.
Returns updated `InsightPostDetail`.

```
POST /intelligence/posts/{post_id}/publish
```
Sets `status='published'` and `published_at=datetime.now(UTC)` if not already
set. Returns updated `InsightPostDetail`. 400 if already published.

```
POST /intelligence/posts/{post_id}/unpublish
```
Sets `status='draft'`, clears `published_at`. Returns updated
`InsightPostDetail`. Useful for pulling a post before archiving.

```
POST /intelligence/posts/{post_id}/archive
```
Sets `status='archived'`. Does not hard-delete. Returns updated
`InsightPostDetail`.

```
GET /intelligence/posts/drafts
```
Returns `PaginatedResponse[InsightPostListItem]` for all non-published posts
(draft + archived). For admin use only.

---

## Route registration

After creating the new routers, register them in the main app router file
(likely `app/api/__init__.py` or `app/main.py`):

```python
from app.api.routes.market_scores import router as market_scores_router
from app.api.routes.intelligence import router as intelligence_router

app.include_router(market_scores_router, prefix="/api/v1")
app.include_router(intelligence_router, prefix="/api/v1")
```

---

## What NOT to build in these phases

Do not start any of the following — they belong to Phase 5 (company config):
- `company_chemistries` junction table or migration
- Company exposure configuration API (`/companies/{id}/exposures`)
- Company overlay scoring endpoint
- Any UI that requires a company to be configured

---

## Key patterns to follow

**404 helper pattern** (see `chemistries.py`):
```python
def _get_post_or_404(db: Session, post_id: int) -> InsightPost:
    post = db.get(InsightPost, post_id)
    if post is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return post
```

**Pagination pattern** (see `materials.py`):
```python
total = db.scalar(select(func.count()).select_from(q.subquery()))
rows = db.scalars(q.order_by(...).offset((page - 1) * limit).limit(limit)).all()
return PaginatedResponse(data=[...], total=total or 0, page=page, limit=limit)
```

**DISTINCT ON for latest-per-group** (Postgres specific):
```python
from sqlalchemy import text
# Latest score per geography for a material:
stmt = (
    select(MaterialGeographyRiskScore)
    .where(MaterialGeographyRiskScore.material_id == material_id)
    .distinct(MaterialGeographyRiskScore.geography_code)
    .order_by(
        MaterialGeographyRiskScore.geography_code,
        MaterialGeographyRiskScore.as_of_date.desc(),
    )
)
```

**Transaction discipline for scoring triggers:**
`score_all_active_materials()` and `rescore_all_chemistries()` manage their
own commits internally. Do NOT wrap them in `with db.begin()` or call
`db.commit()` after them — this will cause double-commit errors.

---

## Acceptance criteria

Phase 1 done when:
- `GET /chemistries/{id}` returns composition table + latest risk score with
  real `trade_volatility` values (not all `0.3`) in `metadata_json`
- `POST /chemistries/rescore` runs without error and returns updated scores
- `GET /chemistries/{id}/risk/history` returns score rows

Phase 2 done when:
- `GET /materials/{id}/market-scores` returns scores for all geographies
- `POST /market/rescore` writes new rows to `material_geography_risk_scores`
- `GET /market/scores?geography_code=CN` returns filtered results

Phase 3 done when:
- `bdi-ingest rescore-market` runs without error and prints a scored-pairs count
- Inngest dev server shows the two cron functions registered
- Search for `RiskEventCompany` inserts in ingestion code — all guarded by
  the `LINK_EVENTS_TO_COMPANIES = False` flag

Phase 4 done when:
- `POST /intelligence/posts` creates a draft post
- `POST /intelligence/posts/{id}/publish` sets status + published_at
- `GET /intelligence/posts` returns published posts (no auth required)
- `GET /intelligence/risk-summary` returns material risk bars (no auth required)
