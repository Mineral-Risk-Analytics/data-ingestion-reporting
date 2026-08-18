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
    ) -> tuple[int | None, int | None, float | None]:
        """
        Return ``(material_id, hs_mapping_id, confidence)`` for an HS code string.

        Implements a longest-prefix match against ``hs_code_material_mappings``
        (market_scope='global').  Tries progressively shorter prefixes in the
        order 10 → 8 → 6 → 4 digits until a match is found.  This covers both
        US HTS 10-digit codes (first 6 digits are the international HS-6 by
        treaty) and standard 4/6-digit UN Comtrade codes.

        Disambiguation rules (applied at each prefix length):
          - Exactly one match → return (material_id, id, confidence).
          - Multiple matches → take the row with the highest confidence.
            If tied at the same confidence → return (None, None, None):
            genuinely ambiguous, not safe to assign.

        Returns (None, None, None) if no mapping is found at any prefix length.

        2026-05-09 (Tier 1.4 audit): added the third ``confidence`` element so
        callers can downscale ``RiskEventMaterial.relevance_score`` for
        low-confidence mappings.  Callers that don't need confidence can
        ignore it (``mid, hs_id, _ = resolver.resolve_by_hs_code(code)``).
        """
        if not hs_code:
            return None, None, None

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
                return rows[0].material_id, rows[0].id, float(rows[0].confidence)

            # Multiple mappings at this prefix: take highest confidence.
            max_conf = max(r.confidence for r in rows)
            top = [r for r in rows if r.confidence == max_conf]
            if len(top) == 1:
                return top[0].material_id, top[0].id, float(top[0].confidence)
            # Tied confidence → genuinely ambiguous at this granularity.
            return None, None, None

        return None, None, None

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

    Single source of truth for free-text → material keyword matching.
    Used by:
      * ingest_federal_register.py  (US regulation event attribution)
      * iea_policy_tracker.py       (IEA policy entry attribution)
      * pipeline.py                 (generic ingestion pipeline)

    Relevance scoring (May 2026 unification):
      The previous fixed weights (0.90 keyword / 0.85 canonical / 0.50 symbol)
      meant a generic term like "metal" — now in 73 mappings after the auto-
      derived expansion — would fire at 0.90 against every news article
      mentioning the word, tagging dozens of materials with high confidence.
      That's a false-positive farm.

      The new model uses INVERSE-FREQUENCY weighting on keyword matches:

          relevance(kw) = base / sqrt(N_mappings_containing_kw)

      where base is 0.90 for ``hs_code_material_mappings.keywords`` entries.
      Specific keywords (``"lithium hydroxide monohydrate"`` in 1 mapping)
      keep their 0.90 score; generic ones (``"metal"`` in 73) drop to ~0.105.
      Square root softens the decay so even very-generic tokens stay
      informative when they're the only thing matching.

      Canonical names and symbols keep fixed weights — they're 1-per-material
      by definition so frequency adjustment doesn't apply:
        0.85 — material.canonical_name (stage-ambiguous; no hs_mapping_id)
        0.50 — material.symbol_or_code (≥2 chars; short symbols risk
               false positives)

    Stage-specific keyword matches take precedence over canonical_name matches
    in detect(), preserving hs_mapping_id attribution in the result.  Canonical
    name matches still detect materials that have no HS mapping yet (returns
    hs_mapping_id=None).

    This class was originally defined in ingest_federal_register.py (PR 16b) and
    moved here in Phase 2 so that iea_policy_tracker and future ingesters can
    import it without a circular dependency.
    """

    # Base relevance for hs_code_material_mappings.keywords matches before
    # the inverse-frequency adjustment is applied.  Tuned so a unique
    # keyword (N=1) gives full 0.90 confidence.
    _KEYWORD_BASE_RELEVANCE = 0.90
    _CANONICAL_RELEVANCE = 0.85
    _SYMBOL_RELEVANCE = 0.50

    # ── Defense-in-depth keyword filtering ─────────────────────────────────
    # The auto-keyword generator strips bare generic stage words at build
    # time (see scripts/generate_hs_keywords_auto.py::_NOISE_TOKENS), but
    # hand-curated entries in ``_HS_KEYWORDS_BY_MAPPING`` can still slip
    # bare generics in (e.g. ``"oxide"``, ``"refined metal"``).  This
    # second filter at build-time catches those — partner-curated noise
    # gets dropped silently.  All entries are lowercased + stripped before
    # comparison.
    _NOISE_KEYWORDS: frozenset[str] = frozenset({
        # Bare stage / form words
        "ore", "ores", "concentrate", "concentrates",
        "metal", "metals", "ingot", "unwrought", "wrought", "refined",
        "scrap", "waste", "recycled",
        "fabricated", "articles", "wire", "rod", "powder",
        "intermediate", "precursor", "compound",
        "high purity",
        # Bare chemical groups (need material context)
        "oxide", "oxides", "hydroxide", "carbonate", "sulfate", "sulphate",
        "chloride", "fluoride", "phosphate", "nitrate",
        # Multi-word but generic (any material can be "refined metal")
        "refined metal",
    })

    # ── Symbol stop-list ──────────────────────────────────────────────────
    # 2-char chemical symbols that are also common English words.  These
    # would fire on ANY article containing the word, not only when the
    # element is genuinely mentioned.  Filtered out at build time.
    _SYMBOL_STOPLIST: frozenset[str] = frozenset({
        "in",  # Indium  → "lithium carbonate prices in Q4" matches every English text
        "re",  # Rhenium → "re:" in emails, "Re." prefix
        "at",  # (not currently a tracked material, but defensive)
        "as",  # (Arsenic — currently skipped, but defensive)
        "be",  # Beryllium-adjacent; verb form
        "do",  # (defensive)
        "if",  # (defensive)
        "is",  # (defensive)
        "it",  # (defensive)
        "no",  # (defensive)
        "of",  # (defensive)
        "on",  # (defensive)
        "or",  # (defensive)
        "so",  # (defensive)
        "to",  # (defensive)
        "up",  # (defensive)
        "us",  # (defensive)
        # "Mo" (Molybdenum) is a borderline case — could match "Mo." abbreviation
        # but rarely as a standalone English word.  Kept for now; revisit if
        # false positives surface in production logs.
    })

    # Default minimum relevance for ``detect()`` to return a match.  Below
    # this threshold the keyword is treated as too ambiguous to record.
    # 0.10 still admits keywords appearing in up to ~80 mappings, which
    # is permissive — the noise filter is mostly the bare-stage-token
    # exclusion at seed-generation time.  Callers that want stricter
    # filtering pass a higher value at ``detect()`` time.
    DEFAULT_MIN_RELEVANCE = 0.10

    # ── Attribution cap (added 2026-05-09, Tier 1.3 audit) ────────────────
    # Caps applied AFTER per-material best-match selection in ``detect()``.
    #
    # ``DEFAULT_MAX_MATERIALS`` — keep at most N materials per event,
    # ordered by relevance descending.  Catches the "topical event mentions
    # cobalt once" case from over-attributing 2-3 incidental matches.
    #
    # ``DEFAULT_LIST_THRESHOLD`` — when an event matches MORE than this
    # many distinct materials, drop ALL attributions.  Strong signal that
    # the source is a generic critical-minerals list (Federal Register
    # "Critical Materials Assessment" notices, SEC 10-K risk factors
    # mentioning every mineral defensively) rather than an event focused
    # on any one material.  Suppress wholesale rather than attributing
    # 47 weak matches that flood the user's events feed.
    #
    # Both can be overridden per-call via ``detect()`` kwargs for ingester-
    # specific tuning (e.g., a focused trade event might legitimately
    # mention 4-5 materials and shouldn't be dropped).
    DEFAULT_MAX_MATERIALS = 3
    DEFAULT_LIST_THRESHOLD = 5

    def __init__(self, entries: list[tuple[str, int, float, int | None]]) -> None:
        import re

        # entries: [(keyword_lower, material_id, relevance, hs_mapping_id|None)]
        self._entries = entries
        # Pre-compile word-boundary regex per unique keyword.  Substring
        # matching ("if 'ore' in lower") was a false-positive farm — it
        # matched in "before", "store", "more".  ``\b`` enforces real
        # word boundaries.  For multi-word keywords the boundaries fall
        # at the start of the first word and end of the last, which is
        # exactly what we want.
        self._patterns: dict[str, "re.Pattern[str]"] = {}
        for kw, *_ in entries:
            if kw not in self._patterns:
                self._patterns[kw] = re.compile(
                    r"\b" + re.escape(kw) + r"\b", re.IGNORECASE,
                )

    @classmethod
    def build(cls, session: Session) -> "MaterialCache":
        """
        Query hs_code_material_mappings (market_scope='global') joined with
        materials to build the keyword → (material_id, relevance, hs_mapping_id)
        lookup.  Materials with no global HS mapping are included as canonical_name
        / symbol fallbacks with hs_mapping_id=None.

        Pass 1 counts how many mappings each keyword appears in (for the
        inverse-frequency weighting).  Pass 2 emits the entries with
        per-keyword relevance computed from the count.
        """
        import math

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
            # Defense-in-depth: drop bare generic stage/chemical-group words
            # (``"ore"``, ``"oxide"``, ``"refined metal"``) and English-word
            # chemical-symbol collisions (``"in"`` for Indium, ``"re"`` for
            # Rhenium) even if they slipped past the auto-generator's
            # _NOISE_TOKENS filter or were added by a partner-curated entry.
            # Cheap O(1) frozenset lookups; runs once at build time.
            if kw in cls._NOISE_KEYWORDS or kw in cls._SYMBOL_STOPLIST:
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

        # Pass 1: count keyword frequency across all mappings.  Used to
        # compute inverse-frequency relevance per keyword in pass 2.
        kw_counts: dict[str, int] = {}
        for _hs_id, keywords_json, _mat_id, _cn, _sym in mapping_rows:
            if keywords_json and isinstance(keywords_json, list):
                for kw in keywords_json:
                    if isinstance(kw, str):
                        kl = kw.lower().strip()
                        if kl and len(kl) >= 3:
                            kw_counts[kl] = kw_counts.get(kl, 0) + 1

        # Pass 2: emit keyword entries with frequency-adjusted relevance.
        for hs_id, keywords_json, mat_id, canonical_name, symbol_or_code in mapping_rows:
            covered_material_ids.add(mat_id)

            # Inverse-frequency-weighted keywords (stage-specific, with hs_mapping_id)
            if keywords_json and isinstance(keywords_json, list):
                for kw in keywords_json:
                    if isinstance(kw, str):
                        kl = kw.lower().strip()
                        n = kw_counts.get(kl, 1)
                        relevance = cls._KEYWORD_BASE_RELEVANCE / math.sqrt(n)
                        _add(kw, mat_id, relevance, hs_id)

            # canonical_name → fixed 0.85, stage-ambiguous (no hs_mapping_id).
            # Frequency-adjustment skipped: canonicals are 1-per-material.
            _add(canonical_name, mat_id, cls._CANONICAL_RELEVANCE, None)

            # 2026-05-12: also register a parenthetical-stripped form so a
            # plain "phosphate" mention in text matches "Phosphate (Battery
            # Grade)".  Unblocks keyword scan for launch-list materials whose
            # canonical names carry a grade qualifier — Phosphate (Battery
            # Grade), Iron Ore, Silicon (Anode Grade).
            #
            # NOTE: this path BYPASSES the _add()-internal _NOISE_KEYWORDS /
            # _SYMBOL_STOPLIST filters.  Those filters exist to drop generic
            # stage/chemical-group words harvested from keywords_json (e.g.
            # bare "ore", "oxide", "phosphate") — but when "Phosphate" is a
            # partner-curated canonical material name, it should remain
            # detectable.  Otherwise the fix above silently drops Phosphate
            # at line 286 because "phosphate" happens to be in _NOISE_KEYWORDS
            # for the unrelated keyword-array case.  Same bypass applies if a
            # future canonical's stripped form collides (e.g. "Oxide
            # (Cathode Grade)" → "Oxide" — which is in _NOISE_KEYWORDS today).
            stripped = cls._strip_parenthetical_suffix(canonical_name)
            if stripped:
                stripped_lower = stripped.lower().strip()
                if (
                    stripped_lower
                    and stripped_lower != canonical_name.lower().strip()
                    and len(stripped_lower) >= 3
                ):
                    entries.append(
                        (stripped_lower, mat_id, cls._CANONICAL_RELEVANCE, None)
                    )

            # symbol (e.g. "Li", "Co") → fixed 0.50; short symbols risk false positives.
            if symbol_or_code and len(symbol_or_code) >= 2:
                _add(symbol_or_code, mat_id, cls._SYMBOL_RELEVANCE, None)

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
            _add(canonical_name, mat_id, cls._CANONICAL_RELEVANCE, None)
            # Same parenthetical-stripped registration applied to the fallback
            # path, with the same noise-filter bypass (see the primary loop
            # for the full rationale).
            stripped = cls._strip_parenthetical_suffix(canonical_name)
            if stripped:
                stripped_lower = stripped.lower().strip()
                if (
                    stripped_lower
                    and stripped_lower != canonical_name.lower().strip()
                    and len(stripped_lower) >= 3
                ):
                    entries.append(
                        (stripped_lower, mat_id, cls._CANONICAL_RELEVANCE, None)
                    )
            if symbol_or_code and len(symbol_or_code) >= 2:
                _add(symbol_or_code, mat_id, cls._SYMBOL_RELEVANCE, None)

        return cls(entries)

    @staticmethod
    def _strip_parenthetical_suffix(name: str) -> str:
        """Strip a trailing ``" (...)"`` qualifier from a canonical name.

        Examples (input → output):
            "Phosphate"  → "Phosphate"
            "Iron Ore"       → "Iron Ore"
            "Silicon (Anode Grade)"      → "Silicon"
            "Lithium"                    → "Lithium"   (unchanged)
            "(Header) name with (suffix)"→ "(Header) name with"  (only trailing)

        Only strips a parenthetical at the END of the name to avoid breaking
        names like "(beta) phase" that might appear in research literature.
        """
        import re

        return re.sub(r"\s*\([^)]*\)\s*$", "", name or "").strip()

    def detect(
        self,
        text: str,
        *,
        min_relevance: float | None = None,
        max_materials: int | None = None,
        list_threshold: int | None = None,
    ) -> list[tuple[int, float, str, int | None]]:
        """
        Scan text for material keywords.

        Returns [(material_id, relevance_score, matched_keyword, hs_mapping_id)],
        sorted by relevance descending, capped at ``max_materials``.
        Per-material selection rules:
          - relevance_score: highest score across all matching keywords
          - hs_mapping_id:   prefer non-None (stage attribution) even when the
                             stage-specific keyword scores slightly lower than the
                             canonical_name match

        Matching is word-bounded (``\\b`` regex), so ``"ore"`` does not
        match inside ``"before"`` and ``"Cu"`` does not match inside
        ``"rescue"``.  Multi-word phrases match as substrings between
        word boundaries on each end.

        Attribution caps (added 2026-05-09, Tier 1.3 audit):
          - ``max_materials`` (default 3): keep only the top-N materials
            by relevance.  Catches incidental mentions in event titles /
            summaries.
          - ``list_threshold`` (default 5): when MORE than this many
            distinct materials match, return an EMPTY list.  Strong
            signal that the source is a generic critical-minerals list
            (Federal Register "Critical Materials Assessment" notices,
            SEC 10-K defensive risk-factor sections) rather than a
            focused event.

        Args:
            text:           The text to scan.
            min_relevance:  Drop matches whose final score falls below
                            this threshold.  Defaults to
                            ``DEFAULT_MIN_RELEVANCE`` (0.10).  Set
                            higher for strict attribution (e.g. 0.30
                            requires either a unique keyword or the
                            canonical name); set to 0.0 to retain
                            every match.
            max_materials:  Max number of materials returned per event,
                            ordered by relevance.  Defaults to
                            ``DEFAULT_MAX_MATERIALS`` (3).  Pass a large
                            number to disable.
            list_threshold: When more than this many distinct materials
                            match, drop ALL attributions wholesale.
                            Defaults to ``DEFAULT_LIST_THRESHOLD`` (5).
                            Pass a large number (e.g. 999) to disable.
        """
        if not text:
            return []

        threshold = self.DEFAULT_MIN_RELEVANCE if min_relevance is None else min_relevance
        cap = self.DEFAULT_MAX_MATERIALS if max_materials is None else max_materials
        list_cap = self.DEFAULT_LIST_THRESHOLD if list_threshold is None else list_threshold

        # material_id → (score, keyword, hs_mapping_id)
        best: dict[int, tuple[float, str, int | None]] = {}

        # Track which keywords we've already searched to avoid duplicating
        # regex work across multiple entries that share a keyword
        # (e.g. "battery grade" appears on many materials).
        kw_present: dict[str, bool] = {}

        for keyword, mat_id, relevance, hs_mapping_id in self._entries:
            if relevance < threshold:
                continue  # filtered out by threshold; cheap exit
            if keyword not in kw_present:
                kw_present[keyword] = bool(self._patterns[keyword].search(text))
            if not kw_present[keyword]:
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

        # ── Apply Tier 1.3 attribution caps ──────────────────────────────
        # 1. Drop all if >list_threshold materials matched (generic list signal).
        if len(best) > list_cap:
            return []
        # 2. Sort by relevance descending and cap at max_materials.
        results = [
            (mat_id, score, kw, hs_id)
            for mat_id, (score, kw, hs_id) in best.items()
        ]
        results.sort(key=lambda r: r[1], reverse=True)
        return results[:cap]
