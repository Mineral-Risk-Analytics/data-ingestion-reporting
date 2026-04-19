"""Shared helpers for every reviewer.

- :func:`score_to_relevance` — bucket an integer score into a relevance label.
- :func:`as_utc_datetime` — promote a ``date`` to an offset-aware UTC
  ``datetime``. Required because all timestamp columns in this project are
  ``TIMESTAMPTZ`` (offset-aware), so naive datetimes cannot be compared
  against ``Company.updated_at`` / ``RiskEvent.event_date`` /
  ``SourceDocument.published_at`` etc. without raising
  ``TypeError: can't compare offset-naive and offset-aware datetimes``.

Buckets
-------
Each reviewer computes a small integer score from its signals (typically 0-4).
That score is mapped to one of ``high`` / ``medium`` / ``low`` / ``none``:

    >=3  -> high
     2   -> medium
     1   -> low
     0   -> none   (not emitted)

The ``max_bucket`` argument lets weak-signal reviewers pin the ceiling. For
example, the facility reviewer calls ``score_to_relevance(score, max_bucket="medium")``
because text co-occurrence is inherently noisy and shouldn't ever reach ``high``.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Literal, Optional

Relevance = Literal["high", "medium", "low", "none"]

_ORDER: tuple[Relevance, ...] = ("none", "low", "medium", "high")
_INDEX: dict[Relevance, int] = {r: i for i, r in enumerate(_ORDER)}


def score_to_relevance(score: int, max_bucket: Relevance = "high") -> Relevance:
    """Bucket an integer score into ``high`` / ``medium`` / ``low`` / ``none``.

    The result is capped by ``max_bucket`` so weak-signal reviewers can pin
    themselves below ``high``.
    """
    if score >= 3:
        raw: Relevance = "high"
    elif score == 2:
        raw = "medium"
    elif score == 1:
        raw = "low"
    else:
        raw = "none"

    if _INDEX[raw] <= _INDEX[max_bucket]:
        return raw
    return max_bucket


def as_utc_datetime(value: Optional[date | datetime]) -> Optional[datetime]:
    """Return a UTC-aware ``datetime`` for ``value`` (or ``None``).

    - ``None`` -> ``None``.
    - ``date`` (not a ``datetime``) -> midnight UTC on that date.
    - naive ``datetime`` -> assumed UTC.
    - aware ``datetime`` -> returned unchanged.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    return datetime.combine(value, time.min, tzinfo=timezone.utc)
