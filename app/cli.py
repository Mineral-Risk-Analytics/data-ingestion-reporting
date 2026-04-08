"""CLI for ingestion and seed (`python -m app.cli` or `bdi-ingest`)."""

from __future__ import annotations

import json
from typing import Optional

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.logging import configure_logging
from app.db.seed import seed_if_empty
from app.db.session import get_session_factory
from app.models import Source
from app.models.enums import SourceType
from app.services.ingestion.pipeline import IngestionPipeline

configure_logging()
app = typer.Typer(no_args_is_help=True)


def _session() -> Session:
    return get_session_factory()()


@app.command("seed")
def seed_cmd() -> None:
    """Load reference countries, materials, suppliers, aliases, and sources."""
    s = _session()
    try:
        stats = seed_if_empty(s)
        typer.echo(json.dumps({"ok": True, "inserted": stats}, indent=2))
    finally:
        s.close()


@app.command("ingest")
def ingest_cmd(
    source: str = typer.Argument(
        ...,
        help="federal-register | census-trade | sec-edgar | news",
    ),
    extra_json: Optional[str] = typer.Option(
        None,
        "--params",
        help='JSON object merged into source config, e.g. \'{"per_page":5}\'',
    ),
) -> None:
    """Run a Phase 1 ingestion pipeline by source alias."""
    mapping = {
        "federal-register": SourceType.FEDERAL_REGISTER.value,
        "census-trade": SourceType.CENSUS_TRADE.value,
        "sec-edgar": SourceType.SEC_EDGAR.value,
        "news": SourceType.NEWS.value,
    }
    st = mapping.get(source.replace("_", "-"))
    if not st:
        typer.echo(f"Unknown source {source!r}. Choose from {list(mapping.keys())}", err=True)
        raise typer.Exit(code=1)

    params = json.loads(extra_json) if extra_json else None

    s = _session()
    try:
        row = s.execute(select(Source).where(Source.source_type == st).limit(1)).scalar_one_or_none()
        if row is None:
            typer.echo("No matching Source row — run `seed` first.", err=True)
            raise typer.Exit(code=1)
        run_id = IngestionPipeline(s).run(row.id, params=params or {})
        typer.echo(json.dumps({"ok": True, "ingestion_run_id": run_id, "source_id": row.id}))
    finally:
        s.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
