"""Parse news article payloads (Phase 1 stub provider + future APIs)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ParsedArticle:
    external_id: str
    title: str
    source_name: str | None
    url: str | None
    published_at: datetime | None
    body_text: str | None
    entities: list[dict[str, Any]]
    event_labels: list[str]
    raw: dict[str, Any] = field(default_factory=dict)


def parse_article(record: dict[str, Any]) -> ParsedArticle:
    published = None
    raw_pub = record.get("published_at")
    if isinstance(raw_pub, str):
        try:
            published = datetime.fromisoformat(raw_pub.replace("Z", "+00:00"))
        except ValueError:
            published = None

    entities = []
    if isinstance(record.get("entities"), list):
        entities = [e for e in record["entities"] if isinstance(e, dict)]

    labels = []
    if isinstance(record.get("event_classification"), list):
        labels = [str(x) for x in record["event_classification"]]
    elif record.get("event_classification"):
        labels = [str(record["event_classification"])]

    return ParsedArticle(
        external_id=str(record.get("id") or record.get("article_id") or ""),
        title=str(record.get("title") or ""),
        source_name=record.get("source"),
        url=record.get("url"),
        published_at=published,
        body_text=record.get("body") or record.get("body_text"),
        entities=entities,
        event_labels=labels,
        raw={k: v for k, v in record.items() if k not in {"body", "body_text"}},
    )
