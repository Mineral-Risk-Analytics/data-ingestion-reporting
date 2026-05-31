"""Resolve external regulation identifiers to canonical regulation rows
via ``regulation_aliases``.

Mirrors ``app.services.ingestion.material_resolver.MaterialAliasResolver``
for the regulations schema.

Usage from an ingester
----------------------
    resolver = RegulationAliasResolver(session)
    result = resolver.resolve("eurlex_celex", "32023R1542")
    if result.status == "ok":
        regulation = result.regulation
        # update mutable fields, attach RiskEvent, etc.
    elif result.status == "skipped":
        continue
    elif result.status == "unknown":
        # surface a warning — partner needs to add an alias row
        # (or a new regulation + alias) before this external ID
        # can be tracked.
        log.warning("unknown CELEX", celex=...)

Performance
-----------
The resolver loads all aliases for a given ``source_system`` on first
call and caches them in-process.  ~5–20 rows per source system is the
expected size — trivial.  Subsequent lookups are O(1) dict hits.

Cache invalidation: callers that hold a resolver across long-running
processes should call ``resolver.refresh()`` after seeding/updating
``regulation_aliases``.

Normalisation
-------------
Lookups are normalised the same way as the unique expression index in
migration 039: ``lower(btrim(source_key))``.  CELEX numbers written as
``" 32023R1542 "`` resolve identically to ``"32023r1542"``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Optional

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.regulatory import Regulation, RegulationSourceAlias

log = structlog.get_logger(__name__)

ResolveStatus = Literal["ok", "skipped", "unknown", "staged"]
# "staged" is returned by resolve_or_stage when an unknown alias was just
# auto-created as a suggested (verified=false) regulation.  Lets callers
# distinguish "we already had this" from "we just created a placeholder."


@dataclass
class ResolveResult:
    """Result of a single regulation alias lookup.

    Fields:
      regulation:  Resolved Regulation row, or None when status != "ok".
      status:      "ok" / "skipped" / "unknown".
    """

    regulation: Optional[Regulation]
    status: ResolveStatus


class RegulationAliasResolver:
    """In-process cache + lookup helper for ``regulation_aliases``."""

    def __init__(self, session: Session) -> None:
        self._session = session
        # cache[source_system][normalised_key] = (Regulation | None, is_skipped)
        self._cache: dict[
            str, dict[str, tuple[Optional[Regulation], bool]]
        ] = {}

    def _ensure_loaded(self, source_system: str) -> None:
        if source_system in self._cache:
            return
        rows = self._session.scalars(
            select(RegulationSourceAlias).where(
                RegulationSourceAlias.source_system == source_system
            )
        ).all()
        bucket: dict[str, tuple[Optional[Regulation], bool]] = {}
        for row in rows:
            key = row.source_key.strip().lower()
            bucket[key] = (row.regulation, row.is_skipped)
        self._cache[source_system] = bucket

    def resolve(
        self, source_system: str, source_key: str
    ) -> ResolveResult:
        """Resolve ``source_key`` under ``source_system``.

        Returns a ``ResolveResult`` with status:
          "ok"        — alias resolves to a tracked regulation.
          "skipped"   — alias exists but is_skipped=True (partner-curated
                        decision not to ingest).
          "unknown"   — no alias row matches (caller surfaces warning;
                        partner needs to add a row before events from
                        this external ID can be attributed).
        """
        if not source_key:
            return ResolveResult(regulation=None, status="unknown")
        self._ensure_loaded(source_system)
        key = source_key.strip().lower()
        entry = self._cache[source_system].get(key)
        if entry is None:
            return ResolveResult(regulation=None, status="unknown")
        regulation, is_skipped = entry
        if is_skipped:
            return ResolveResult(regulation=None, status="skipped")
        return ResolveResult(regulation=regulation, status="ok")

    def resolve_or_raise(
        self, source_system: str, source_key: str
    ) -> Regulation:
        """Convenience: resolve and raise on skipped/unknown.

        Useful in code paths where the caller has already filtered out
        skipped rows and an unknown alias is a real error.
        """
        result = self.resolve(source_system, source_key)
        if result.status == "ok":
            assert result.regulation is not None
            return result.regulation
        if result.status == "skipped":
            raise SkippedAliasError(
                f"Alias ({source_system!r}, {source_key!r}) is marked is_skipped=True"
            )
        raise UnknownAliasError(
            f"No alias for ({source_system!r}, {source_key!r}) — add a row "
            f"to seed_regulation_aliases.py and re-seed."
        )

    def resolve_or_stage(
        self,
        source_system: str,
        source_key: str,
        *,
        title_hint: Optional[str] = None,
        context_hint: Optional[str] = None,
    ) -> ResolveResult:
        """Like ``resolve()`` but auto-creates a suggested-regulation
        placeholder when the external ID isn't recognised.

        Three cases that trigger staging:

          1. No alias row exists at all for (source_system, source_key)
             → create a new Regulation (verified=False) + alias row.
          2. Alias row exists but ``regulation_id`` is NULL (the "lazy
             alias" case — partner added an alias but hasn't filled in
             the Regulation row yet) → create a Regulation and link
             the existing alias to it.
          3. Otherwise behaves identically to resolve().

        The auto-created Regulation has:
          * ``verified = False`` — invisible to scoring until partner
            promotes it (the scoring layer filters on verified=True).
          * ``regulation_key`` = ``SUGGESTED_{SOURCE}_{KEY}`` (auto-
            generated; partner renames during promotion).
          * ``status = "proposed"`` (close-enough default; partner sets
            the real status).
          * ``metadata_json`` carries `suggested=True`,
            `source_system`, `source_key`, `first_observed`, and
            ``sample_context`` if provided.

        Returns ``ResolveResult`` with one of:
          * ``status="ok"`` — alias already pointed to a real regulation
          * ``status="skipped"`` — alias is_skipped=True
          * ``status="staged"`` — just auto-created a placeholder (the
            ``regulation`` field holds the new verified=False row)

        ``title_hint`` and ``context_hint`` are optional metadata the
        caller can pass to make the placeholder more useful when partner
        reviews the queue.  E.g., an EUR-Lex auto-discovery scanner
        could pass the regulation's HTML title; a Federal Register
        parser could pass the surrounding document title.
        """
        # Initial resolve attempt — fast path if alias already exists
        # and points to a real regulation.
        result = self.resolve(source_system, source_key)

        # Case A: alias points to a VERIFIED regulation → return as-is.
        # Case A': alias points to a SUGGESTED (verified=False) reg
        # that we previously auto-staged → bump observation tracking
        # before returning, so the queue surfaces "most-observed"
        # accurately even on repeat encounters.
        if result.status == "ok" and result.regulation is not None:
            if (
                not result.regulation.verified
                and (result.regulation.metadata_json or {}).get("suggested")
            ):
                # Repeat encounter of a previously-staged suggestion —
                # delegate to stage_unknown_regulation which already
                # handles the idempotent-bump path.  Returns the same
                # regulation row with observation_count incremented.
                stage_unknown_regulation(
                    self._session,
                    source_system=source_system,
                    source_key=source_key,
                    title_hint=title_hint,
                    context_hint=context_hint,
                )
                return ResolveResult(regulation=result.regulation, status="staged")
            return result

        # Case B: alias is_skipped → respect partner's deliberate
        # decision not to ingest; do NOT auto-stage
        if result.status == "skipped":
            return result

        # Case C: alias exists but regulation_id=NULL ("lazy alias")
        # OR alias does not exist at all ("unknown")
        # → both trigger staging
        new_regulation = stage_unknown_regulation(
            self._session,
            source_system=source_system,
            source_key=source_key,
            title_hint=title_hint,
            context_hint=context_hint,
        )

        # Invalidate the cache for this source_system so the next
        # resolve() picks up the newly-created alias/regulation.
        self._cache.pop(source_system, None)

        return ResolveResult(regulation=new_regulation, status="staged")

    def refresh(self) -> None:
        """Drop the in-process cache.  Call after seeding new aliases."""
        self._cache.clear()


# ---------------------------------------------------------------------------
# Suggested-regulation staging (queue mechanism)
# ---------------------------------------------------------------------------
# Generates regulation_key suffixes that are safe for both human reading
# and database uniqueness.  CELEX numbers like "32023R1542" pass straight
# through; identifiers with slashes or punctuation get normalised.
_SAFE_KEY_RE = re.compile(r"[^A-Za-z0-9_]+")


def _generate_suggested_regulation_key(source_system: str, source_key: str) -> str:
    """Build a stable, partner-readable regulation_key for a suggestion.

    Convention: ``SUGGESTED_{SOURCE}_{KEY}`` (uppercase, non-alphanumerics
    collapsed to underscores, truncated to fit String(256)).

    Example: stage_unknown_regulation("eurlex_celex", "32024R1234")
             → regulation_key = "SUGGESTED_EURLEX_CELEX_32024R1234"

    The SUGGESTED_ prefix makes these rows trivially queryable
    (``WHERE regulation_key LIKE 'SUGGESTED_%'``) and makes it obvious
    to partner reviewers that they're looking at an unverified
    placeholder, not a curated regulation.
    """
    source_part = _SAFE_KEY_RE.sub("_", source_system).upper().strip("_")
    key_part = _SAFE_KEY_RE.sub("_", source_key).upper().strip("_")
    candidate = f"SUGGESTED_{source_part}_{key_part}"
    # regulation_key is String(256); truncate defensively.  Real external
    # IDs are typically ≤32 chars so this rarely fires.
    return candidate[:256]


def stage_unknown_regulation(
    session: Session,
    *,
    source_system: str,
    source_key: str,
    title_hint: Optional[str] = None,
    context_hint: Optional[str] = None,
) -> Regulation:
    """Create a suggested (verified=False) regulation + alias row for
    review by partner.

    Used when an ingester encounters an external regulation identifier
    that doesn't yet have a curated Regulation row.  The new row is
    invisible to scoring (verified=False filter throughout) until
    partner reviews it and promotes it via ``seed_regulations.py``.

    Idempotency: if a regulation with the auto-generated key already
    exists (e.g., from a previous staging run), this function reuses
    it rather than creating a duplicate.  Same for the alias row.

    Args:
        session: SQLAlchemy session (caller commits).
        source_system: e.g. "eurlex_celex", "federal_register_doc"
        source_key: the external identifier (CELEX, FR doc number, etc.)
        title_hint: optional human-readable title.  Partner sees this
                    in the review queue; falls back to a generic
                    placeholder if not provided.
        context_hint: optional surrounding context (event title, URL,
                      etc.) — stored in metadata_json for partner
                      review.

    Returns the new (or pre-existing) Regulation row.  The row IS
    flushed to the session but NOT committed — caller controls the
    transaction boundary.
    """
    auto_key = _generate_suggested_regulation_key(source_system, source_key)
    now_iso = datetime.now(timezone.utc).isoformat()

    # Idempotency: reuse an existing SUGGESTED_ row if we've staged
    # this external ID before.
    existing = session.scalar(
        select(Regulation).where(Regulation.regulation_key == auto_key)
    )
    if existing is not None:
        # Bump the observation count + last_observed timestamp so the
        # queue surfaces "most-observed" suggestions first.
        meta = dict(existing.metadata_json or {})
        meta["last_observed"] = now_iso
        meta["observation_count"] = int(meta.get("observation_count", 1)) + 1
        if context_hint and "sample_contexts" in meta:
            # Keep a small ring buffer of recent contexts (max 5) to
            # help partner triage — different sample contexts may
            # suggest the regulation matters more or less.
            contexts = list(meta.get("sample_contexts") or [])[-4:]
            contexts.append(context_hint[:200])
            meta["sample_contexts"] = contexts
        elif context_hint:
            meta["sample_contexts"] = [context_hint[:200]]
        existing.metadata_json = meta
        session.flush()
        log.debug(
            "regulation_resolver.suggestion_observed",
            regulation_key=auto_key,
            observation_count=meta["observation_count"],
        )
        return existing

    # Create the new suggested-regulation row.
    title = title_hint or f"[Suggested] {source_system}:{source_key}"
    metadata = {
        "suggested": True,
        "source_system": source_system,
        "source_key": source_key,
        "first_observed": now_iso,
        "last_observed": now_iso,
        "observation_count": 1,
    }
    if context_hint:
        metadata["sample_contexts"] = [context_hint[:200]]

    new_reg = Regulation(
        regulation_key=auto_key,
        title=title[:1024],
        issuing_body="Pending partner review",
        geography=None,
        policy_theme="pending_review",
        status="proposed",
        publication_date=None,
        effective_date=None,
        summary=None,
        metadata_json=metadata,
        verified=False,
    )
    session.add(new_reg)
    session.flush()

    # Reconcile the alias.  Three cases:
    #   (a) No alias row exists → create one pointing to the new reg.
    #   (b) Alias row exists with regulation_id=NULL ("lazy alias")
    #       → link it to the new reg.
    #   (c) Alias exists already pointing to another regulation →
    #       this shouldn't happen given resolve_or_stage's contract,
    #       but defensively we leave it alone.
    normalised_key = source_key.strip().lower()
    existing_alias = session.scalar(
        select(RegulationSourceAlias).where(
            RegulationSourceAlias.source_system == source_system,
            # Note: we compare normalised_key against
            # lower(btrim(source_key)) but SQLAlchemy can't model the
            # expression cleanly here.  In Postgres the unique index
            # handles uniqueness; this lookup catches the common case
            # where the source_key already matches case-insensitively.
        )
    )
    matched = None
    if existing_alias is not None:
        # Fall back to a more careful in-Python compare since the SQL
        # doesn't normalise.  Iterate all aliases for the source_system
        # and find one whose normalised key matches.
        all_aliases = session.scalars(
            select(RegulationSourceAlias).where(
                RegulationSourceAlias.source_system == source_system,
            )
        ).all()
        for alias in all_aliases:
            if alias.source_key.strip().lower() == normalised_key:
                matched = alias
                break

    if matched is None:
        # Case (a): create new alias linking external ID to new reg.
        session.add(
            RegulationSourceAlias(
                source_system=source_system,
                source_key=source_key,
                regulation_id=new_reg.id,
                is_skipped=False,
                notes="Auto-staged by resolve_or_stage()",
            )
        )
    elif matched.regulation_id is None:
        # Case (b): lazy alias — fill in the regulation_id.
        matched.regulation_id = new_reg.id
        existing_notes = matched.notes or ""
        matched.notes = (
            existing_notes + "\nLinked to auto-staged regulation by resolve_or_stage()"
        ).strip()
    # Case (c): existing alias points elsewhere — defensive no-op.

    session.flush()
    log.info(
        "regulation_resolver.suggestion_staged",
        regulation_key=auto_key,
        source_system=source_system,
        source_key=source_key,
    )
    return new_reg


def list_suggested_regulations(session: Session) -> list[dict]:
    """Return every suggested (verified=False) regulation as a list of
    review-friendly dicts.

    Used by the ``bdi-ingest list-suggested-regulations`` CLI to
    surface the partner-review queue.  Each dict carries enough
    context for partner to decide approve / reject / merge without
    going back to the source data:

        regulation_key      — the SUGGESTED_* auto-generated key
        title               — what we know about the regulation
        source_system       — e.g. "eurlex_celex"
        source_key          — e.g. "32024R1234"
        first_observed      — ISO timestamp of first encounter
        last_observed       — ISO timestamp of most recent encounter
        observation_count   — how many times we've seen this external ID
        sample_contexts     — short text excerpts from where the ID
                               was observed (max 5)
        next_step           — short hint for partner

    Sorted by observation_count descending (most-observed first —
    these are the most-likely-to-matter regulations).
    """
    rows = session.scalars(
        select(Regulation).where(Regulation.verified.is_(False))
    ).all()

    queue: list[dict] = []
    for reg in rows:
        meta = reg.metadata_json or {}
        if not meta.get("suggested"):
            # Defensive — verified=false rows might exist for reasons
            # other than auto-staging.  Only include ones tagged as
            # suggested by stage_unknown_regulation.
            continue
        queue.append({
            "regulation_key": reg.regulation_key,
            "title": reg.title,
            "source_system": meta.get("source_system"),
            "source_key": meta.get("source_key"),
            "first_observed": meta.get("first_observed"),
            "last_observed": meta.get("last_observed"),
            "observation_count": int(meta.get("observation_count", 1)),
            "sample_contexts": meta.get("sample_contexts", []),
            "next_step": (
                f"Review {meta.get('source_system')}:{meta.get('source_key')}. "
                f"If keeping, add a proper regulation row to "
                f"seed_regulations.py and the alias to "
                f"seed_regulation_aliases.py, then re-seed; the seed "
                f"will reconcile via regulation_key.  If rejecting, "
                f"mark the alias is_skipped=True with a skip_reason."
            ),
        })
    # Sort: most-observed first (likely highest signal)
    queue.sort(key=lambda r: r["observation_count"], reverse=True)
    return queue


class SkippedAliasError(Exception):
    """Raised by resolve_or_raise when the alias is is_skipped=True."""


class UnknownAliasError(Exception):
    """Raised by resolve_or_raise when no alias row matches."""


# Functional convenience for one-off callers that don't want to manage a
# resolver instance.  Builds and discards a one-row cache — fine for
# small CLIs, wasteful in tight loops.
def resolve_regulation(
    session: Session, source_system: str, source_key: str
) -> ResolveResult:
    return RegulationAliasResolver(session).resolve(source_system, source_key)


__all__ = [
    "RegulationAliasResolver",
    "resolve_regulation",
    "stage_unknown_regulation",
    "list_suggested_regulations",
    "ResolveResult",
    "ResolveStatus",
    "SkippedAliasError",
    "UnknownAliasError",
]
