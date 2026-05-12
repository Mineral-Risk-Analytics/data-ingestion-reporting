"""Hybrid keyword + LLM material classifier for free-text events.

Currently used by the SEC EDGAR ingester (Tier 3 audit fix, 2026-05-09)
to improve material attribution on 10-K / 8-K narrative excerpts where
defensive risk-factor language frequently name-drops every critical
mineral without the filing being materially about any of them.

Design — hybrid pre-filter + confirmation:

  1. ``MaterialCache.detect()`` runs as today — cheap, deterministic
     keyword pre-filter that yields a small candidate set (top-3 by
     Tier 1.3 caps).  Most events resolve here with no further work.

  2. When candidates exist AND the source text is substantive (above
     a minimum length), we send the text + candidate-material list to
     Claude Haiku 4.5 with a structured-output tool.  Haiku returns,
     per candidate, whether the filing is genuinely about that material
     (vs incidentally mentioning it) plus a confidence value.

  3. The classifier multiplies the candidate's keyword-relevance by
     Haiku's confidence (0.0 if Haiku rules it out; up to 1.0 if Haiku
     confirms it as central).  Result: a 10-K that mentions "cobalt" in
     a 47-mineral defensive list returns 0 attributions; a 10-K that's
     actually about cobalt supply contracts returns a high-relevance
     Cobalt attribution.

Cost guardrails:

  - Skipped entirely when ``ANTHROPIC_API_KEY`` isn't set (graceful
    degrade to pure-keyword behaviour).
  - Skipped when the keyword pre-filter returns 0 candidates (no
    materials to confirm, nothing to do).
  - Text is truncated to ``_MAX_TEXT_CHARS`` before sending — bounds
    the per-call input tokens.
  - In-process cache keyed by ``(content_hash, candidate_ids)`` so
    repeated runs on the same input don't re-bill.

Fallback semantics: any LLM error (network, parse, validation) is logged
at WARN and the function returns the original keyword matches unchanged.
The pipeline never gets worse than its pure-keyword baseline.

Why Haiku not Sonnet: this is a yes/no-with-confidence classification
over a short candidate list, not a structural reasoning task.  Sonnet's
marginal accuracy gain (~5%) isn't worth ~5× the cost.  The MCS PDF
locator uses Sonnet because section-boundary detection genuinely
benefits from the larger model; classification doesn't.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


# ── Configuration ──────────────────────────────────────────────────────
_MODEL = "claude-haiku-4-5-20251001"
_MAX_TEXT_CHARS = 8_000  # ~2K tokens — captures filing excerpts without runaway cost
_MIN_TEXT_CHARS = 200    # below this, keyword detection is the only signal anyway
_MAX_OUTPUT_TOKENS = 1024
_REQUEST_TIMEOUT_S = 30


_TOOL_SCHEMA = {
    "name": "classify_materials",
    "description": (
        "Classify, for each candidate material, whether the source text is "
        "materially about that material (vs incidentally mentioning it). "
        "Return one entry per candidate.  Materials are 'materially about' "
        "when the text discusses supply, demand, pricing, regulation, "
        "operations, contracts, or specific business risk for them; "
        "'incidentally mentioned' when they appear in a generic critical-"
        "minerals list, a multi-material risk-factor sentence, or as one "
        "among many supply-chain inputs without focused discussion."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "material": {
                            "type": "string",
                            "description": "Material name — must match one of the candidates",
                        },
                        "is_material": {
                            "type": "boolean",
                            "description": (
                                "True if the text is genuinely about this "
                                "material; False if only incidentally mentioned."
                            ),
                        },
                        "confidence": {
                            "type": "number",
                            "description": "Confidence in is_material on [0, 1]",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                        "evidence": {
                            "type": "string",
                            "description": (
                                "Brief phrase (≤80 chars) quoting or paraphrasing "
                                "the key text supporting the verdict.  Empty when "
                                "is_material=false and no specific evidence."
                            ),
                        },
                    },
                    "required": ["material", "is_material", "confidence"],
                },
            },
        },
        "required": ["classifications"],
    },
}


_SYSTEM_PROMPT = (
    "You are a domain expert on critical-minerals supply chains.  Your job "
    "is to decide whether a source text is materially focused on each of a "
    "short list of candidate materials, or only mentions them in passing.  "
    "Return your verdicts via the classify_materials tool.  Bias toward "
    "rejecting incidental mentions — the downstream consumer needs an "
    "events feed that surfaces focused signal, not every defensive risk-"
    "factor name-drop."
)


def _content_key(text: str, candidate_names: list[str]) -> str:
    """Stable cache key for (text, candidates).  Sorted to avoid permutation drift."""
    h = hashlib.sha256()
    h.update(text.encode("utf-8", errors="ignore"))
    h.update(b"|")
    h.update(",".join(sorted(candidate_names)).encode("utf-8"))
    return h.hexdigest()[:24]


class MaterialClassifier:
    """LLM-backed refinement layer over keyword detection.

    Construct once per ingest run.  ``classify()`` returns a refined
    match list given the original keyword matches plus the source text.
    Holds an in-process cache so identical (text, candidate-set) pairs
    are computed once.
    """

    def __init__(
        self,
        *,
        materials_by_id: dict[int, str],
        enabled: Optional[bool] = None,
    ) -> None:
        """Args:
            materials_by_id:  Map material_id → canonical_name.  Used to
                              translate keyword-match material IDs into
                              names for the LLM prompt and back.
            enabled:          If None, auto-detects from ``ANTHROPIC_API_KEY``
                              env var.  Pass False explicitly to force
                              the keyword-only fallback (useful in tests).
        """
        self._materials_by_id = materials_by_id
        self._name_to_id = {name.lower(): mid for mid, name in materials_by_id.items()}
        self._cache: dict[str, list[tuple[int, float, str]]] = {}
        self._client = None
        if enabled is False:
            self._enabled = False
        else:
            self._enabled = bool(os.environ.get("ANTHROPIC_API_KEY")) and enabled is not False
            if not self._enabled:
                log.info(
                    "material_classifier.disabled",
                    reason="no ANTHROPIC_API_KEY in environment",
                )

    @property
    def enabled(self) -> bool:
        return self._enabled

    def classify(
        self,
        text: str,
        keyword_matches: list[tuple[int, float, str, int | None]],
    ) -> list[tuple[int, float, str, int | None]]:
        """Refine keyword matches via Haiku confirmation.

        Args:
            text:              The event text to classify (e.g. filing summary).
            keyword_matches:   Output of ``MaterialCache.detect()`` — list of
                               ``(material_id, relevance, matched_keyword,
                               hs_mapping_id)`` tuples.

        Returns:
            Refined list with the same shape.  Each entry's relevance is
            multiplied by Haiku's confidence; entries Haiku rejects
            (is_material=false with confidence ≥ 0.5) are dropped.

        Failure mode: returns ``keyword_matches`` unchanged on any error
        (LLM disabled, network failure, parse error, no candidates).
        Never raises.
        """
        if not self._enabled:
            return keyword_matches
        if not keyword_matches:
            return keyword_matches
        if not text or len(text) < _MIN_TEXT_CHARS:
            return keyword_matches

        # Build candidate name list (sorted, deduped).
        candidate_names = sorted({
            self._materials_by_id.get(mid, "")
            for mid, _, _, _ in keyword_matches
        } - {""})
        if not candidate_names:
            return keyword_matches

        # Cache lookup before any network round-trip.
        cache_key = _content_key(text[:_MAX_TEXT_CHARS], candidate_names)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return self._apply_decisions(keyword_matches, cached)

        # Call Haiku.
        try:
            decisions = self._call_haiku(text[:_MAX_TEXT_CHARS], candidate_names)
        except Exception:  # noqa: BLE001 — log + fall back, never crash ingest
            log.warning(
                "material_classifier.haiku_call_failed",
                candidates=candidate_names,
                exc_info=True,
            )
            return keyword_matches

        self._cache[cache_key] = decisions
        return self._apply_decisions(keyword_matches, decisions)

    def _apply_decisions(
        self,
        keyword_matches: list[tuple[int, float, str, int | None]],
        decisions: list[tuple[int, float, str]],
    ) -> list[tuple[int, float, str, int | None]]:
        """Merge Haiku decisions back onto the original keyword match tuples.

        ``decisions`` is ``[(material_id, confidence_multiplier, evidence)]``
        where multiplier is 0.0 (rejected) up to 1.0 (strongly confirmed).
        """
        decision_by_mid = {mid: (mult, evidence) for mid, mult, evidence in decisions}
        refined: list[tuple[int, float, str, int | None]] = []
        for mid, relevance, matched_kw, hs_id in keyword_matches:
            mult, evidence = decision_by_mid.get(mid, (1.0, ""))
            if mult <= 0.0:
                continue  # Haiku rejected — drop the attribution entirely
            new_relevance = max(0.0, min(1.0, relevance * mult))
            # Prepend an "llm:" tag to match_reason so downstream callers
            # can distinguish keyword-only matches from Haiku-confirmed ones.
            if evidence:
                kw_label = f"llm_confirmed:{matched_kw[:32]}|{evidence[:40]}"
            else:
                kw_label = f"llm_confirmed:{matched_kw[:48]}"
            refined.append((mid, new_relevance, kw_label[:96], hs_id))
        return refined

    def _call_haiku(
        self,
        text: str,
        candidate_names: list[str],
    ) -> list[tuple[int, float, str]]:
        """Invoke Haiku via Anthropic SDK and return per-material decisions.

        Returns ``[(material_id, confidence_multiplier, evidence)]``.
        ``confidence_multiplier`` is the value to multiply the original
        keyword relevance by: ``confidence`` when ``is_material=True``,
        ``1.0 - confidence`` (clamped to a 0.0 floor when high-confidence
        rejection) when ``is_material=False``.

        Raises on any failure — caller catches and falls back.
        """
        if self._client is None:
            # Lazy SDK import + env loading so the module is importable
            # without anthropic installed (CI, isolated tests).
            from anthropic import Anthropic  # type: ignore

            # Honour project-root .env if not already loaded.
            try:
                from dotenv import load_dotenv  # type: ignore
                load_dotenv()
            except ImportError:
                pass

            self._client = Anthropic(timeout=_REQUEST_TIMEOUT_S)

        candidates_block = "\n".join(f"  - {n}" for n in candidate_names)
        user_text = (
            "Source text:\n"
            "---\n"
            f"{text}\n"
            "---\n\n"
            "Candidate materials (the text has keyword matches for these):\n"
            f"{candidates_block}\n\n"
            "For each candidate, call classify_materials with your verdict."
        )

        response = self._client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=_SYSTEM_PROMPT,
            tools=[_TOOL_SCHEMA],
            tool_choice={"type": "tool", "name": "classify_materials"},
            messages=[{"role": "user", "content": user_text}],
        )

        # Extract the tool_use block.
        tool_block = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if tool_block is None:
            raise RuntimeError("Haiku response missing classify_materials tool_use")
        payload = tool_block.input
        if isinstance(payload, str):
            payload = json.loads(payload)
        classifications = payload.get("classifications", [])

        decisions: list[tuple[int, float, str]] = []
        for c in classifications:
            name = (c.get("material") or "").strip()
            mid = self._name_to_id.get(name.lower())
            if mid is None:
                # Haiku returned a name not in the candidate set — skip.
                continue
            is_mat = bool(c.get("is_material"))
            conf = float(c.get("confidence") or 0.0)
            conf = max(0.0, min(1.0, conf))
            evidence = (c.get("evidence") or "").strip()
            # Translate to a multiplier on the keyword relevance:
            #   - is_material=True  → conf (scales keyword relevance up or down)
            #   - is_material=False with high confidence (≥0.5) → 0.0 (drop)
            #   - is_material=False with low confidence → keep at 0.5 (uncertain)
            if is_mat:
                multiplier = conf
            elif conf >= 0.5:
                multiplier = 0.0
            else:
                multiplier = 0.5
            decisions.append((mid, multiplier, evidence))

        log.debug(
            "material_classifier.haiku_decisions",
            candidates=candidate_names,
            decisions=[(self._materials_by_id.get(m, str(m)), mult)
                       for m, mult, _ in decisions],
        )
        return decisions


__all__ = ["MaterialClassifier"]
