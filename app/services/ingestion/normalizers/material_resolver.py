"""
Map HS codes / keywords to `materials` rows (DB-backed with longest-prefix match).

Phase 1.5: _HS_PREFIX_RULES removed.  resolve_by_hs_code() queries
hs_code_material_mappings directly, implementing a longest-prefix match
(10→8→6→4 digits) that returns (material_id, hs_mapping_id) so callers
can persist stage attribution alongside material attribution.

Phase 2: MaterialCache moved here from ingest_federal_register.py so that
other ingesters (iea_policy_tracker, future news ingesters) can import it
without creating a circular dependency.

To add or update HS prefix → material mappings, update
hs_code_material_mappings via seed_hs_mappings.py — not this file.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.supply import HsCodeMaterialMapping, Material

# NOTE: _HS_PREFIX_RULES was removed in Phase 1.5 (PR 16e).
# All prefix → material logic is now in hs_code_material_mappings (DB).


class MaterialResolver:
    def __init__(self, db: Session) -> None:
        self._db = db

    def resolve_by_hs_code(
        self, hs_code: str | None
    ) -> tuple[int | None, int | None]:
        """
        Return ``(material_id, hs_mapping_id)`` for an HS code string.

        Implements a longest-prefix match against ``hs_code_material_mappings``
        (market_scope='global').  Tries progressively shorter prefixes in the
        order 10 → 8 → 6 → 4 digits until a match is found.  This covers both
        US HTS 10-digit codes (first 6 digits are the international HS-6 by
        treaty) and standard 4/6-digit UN Comtrade codes.

        Disambiguation rules (applied at each prefix length):
          - Exactly one match → return (material_id, id).
          - Multiple matches → take the row with the highest confidence.
            If tied at the same confidence → return (None, None): genuinely
            ambiguous, not safe to assign.

        Returns (None, None) if no mapping is found at any prefix length.

        NOTE: changed from ``int | None`` in Phase 1 — callers must unpack
        the tuple.  The old signature returned material_id only.
        """
        if not hs_code:
            return None, None

        # Normalise: strip whitespace and dots (DB prefix may be stored as
        # "85.07" or "8507"; we compare without dots either way).
        code = str(hs_code).strip().replace(".", "")

        for length in (10, 8, 6, 4):
            if len(code) < length:
                continue  # code is shorter than this prefix level

            prefix = code[:length]
            rows = self._db.execute(
                select(
                    HsCodeMaterialMapping.id,
                    HsCodeMaterialMapping.material_id,
                    HsCodeMaterialMapping.confidence,
                ).where(
                    HsCodeMaterialMapping.hs_code_prefix == prefix,
                    HsCodeMaterialMapping.market_scope == "global",
                )
            ).all()

            if not rows:
                continue

            if len(rows) == 1:
                return rows[0].material_id, rows[0].id

            # Multiple mappings at this prefix: take highest confidence.
            max_conf = max(r.confidence for r in rows)
            top = [r for r in rows if r.confidence == max_conf]
            if len(top) == 1:
                return top[0].material_id, top[0].id
            # Tied confidence → genuinely ambiguous at this granularity.
            return None, None

        return None, None

    def resolve_by_canonical_name(self, name: str) -> Material | None:
        return self._db.execute(
            select(Material).where(Material.canonical_name == name)
        ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# MaterialCache — keyword-based material detection
# ---------------------------------------------------------------------------

class MaterialCache:
    """
    Pre-built lookup of material keywords → (material_id, relevance_score,
    hs_mapping_id).  Built once per ingest run from hs_code_material_mappings
    + materials and reused across events.

    Keyword sources and their relevance weights:
      0.90  — keywords from hs_code_material_mappings.keywords (stage-specific,
              loaded from DB — replaces the old hard-coded _MATERIAL_ALIASES dict)
      0.85  — material.canonical_name (stage-ambiguous; no hs_mapping_id assigned)
      0.50  — material.symbol_or_code (high false-positive risk for short symbols)

    Stage-specific keyword matches take precedence over canonical_name matches
    in detect(), preserving hs_mapping_id attribution in the result.  Canonical
    name matches still detect materials that have no HS mapping yet (returns
    hs_mapping_id=None).

    This class was originally defined in ingest_federal_register.py (PR 16b) and
    moved here in Phase 2 so that iea_policy_tracker and future ingesters can
    import it without a circular dependency.
    """

    def __init__(self, entries: list[tuple[str, int, float, int | None]]) -> None:
        # entries: [(keyword_lower, material_id, relevance, hs_mapping_id|None)]
        self._entries = entries

    @classmethod
    def build(cls, session: Session) -> "MaterialCache":
        """
        Query hs_code_material_mappings (market_scope='global') joined with
        materials to build the keyword → (material_id, relevance, hs_mapping_id)
        lookup.  Materials with no global HS mapping are included as canonical_name
        / symbol fallbacks with hs_mapping_id=None.
        """
        entries: list[tuple[str, int, float, int | None]] = []

        def _add(
            keyword: str,
            mat_id: int,
            relevance: float,
            hs_mapping_id: int | None,
        ) -> None:
            kw = keyword.lower().strip()
            if not kw or len(kw) < 3:
                return
            entries.append((kw, mat_id, relevance, hs_mapping_id))

        # ── Primary source: hs_code_material_mappings (market_scope='global') ──
        # Provides hs_mapping_id for stage attribution on detected events.
        mapping_rows = session.execute(
            select(
                HsCodeMaterialMapping.id,
                HsCodeMaterialMapping.keywords,
                Material.id.label("material_id"),
                Material.canonical_name,
                Material.symbol_or_code,
            )
            .join(Material, HsCodeMaterialMapping.material_id == Material.id)
            .where(HsCodeMaterialMapping.market_scope == "global")
        ).all()

        covered_material_ids: set[int] = set()

        for hs_id, keywords_json, mat_id, canonical_name, symbol_or_code in mapping_rows:
            covered_material_ids.add(mat_id)

            # Stage-specific keywords from DB → 0.90, tagged with hs_mapping_id
            if keywords_json and isinstance(keywords_json, list):
                for kw in keywords_json:
                    if isinstance(kw, str):
                        _add(kw, mat_id, 0.90, hs_id)

            # canonical_name → 0.85, stage-ambiguous (no hs_mapping_id)
            _add(canonical_name, mat_id, 0.85, None)

            # symbol (e.g. "Li", "Co") → 0.50; short symbols risk false positives
            if symbol_or_code and len(symbol_or_code) >= 2:
                _add(symbol_or_code, mat_id, 0.50, None)

        # ── Fallback: materials not covered by any global HS mapping ─────────
        # These still need to be detectable; they just have no stage attribution.
        fallback_stmt = select(
            Material.id, Material.canonical_name, Material.symbol_or_code
        )
        if covered_material_ids:
            fallback_stmt = fallback_stmt.where(
                Material.id.notin_(covered_material_ids)
            )
        for mat_id, canonical_name, symbol_or_code in session.execute(fallback_stmt).all():
            _add(canonical_name, mat_id, 0.85, None)
            if symbol_or_code and len(symbol_or_code) >= 2:
                _add(symbol_or_code, mat_id, 0.50, None)

        return cls(entries)

    def detect(self, text: str) -> list[tuple[int, float, str, int | None]]:
        """
        Scan text for material keywords.

        Returns [(material_id, relevance_score, matched_keyword, hs_mapping_id)],
        one entry per material.  Per-material selection rules:
          - relevance_score: highest score across all matching keywords
          - hs_mapping_id:   prefer non-None (stage attribution) even when the
                             stage-specific keyword scores slightly lower than the
                             canonical_name match
        """
        lower = text.lower()
        # material_id → (score, keyword, hs_mapping_id)
        best: dict[int, tuple[float, str, int | None]] = {}

        for keyword, mat_id, relevance, hs_mapping_id in self._entries:
            if keyword not in lower:
                continue
            existing = best.get(mat_id)
            if existing is None:
                best[mat_id] = (relevance, keyword, hs_mapping_id)
            else:
                ex_score, ex_kw, ex_hs_id = existing
                if relevance > ex_score:
                    # Higher score wins; keep any hs_mapping_id that was found
                    new_hs_id = hs_mapping_id if hs_mapping_id is not None else ex_hs_id
                    best[mat_id] = (relevance, keyword, new_hs_id)
                elif hs_mapping_id is not None and ex_hs_id is None:
                    # Same or lower score but this match carries stage attribution
                    best[mat_id] = (ex_score, ex_kw, hs_mapping_id)

        return [
            (mat_id, score, kw, hs_id)
            for mat_id, (score, kw, hs_id) in best.items()
        ]
