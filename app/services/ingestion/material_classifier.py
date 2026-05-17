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


_RELEVANCE_TOOL_SCHEMA = {
    "name": "assess_battery_industry_relevance",
    "description": (
        "Decide whether a policy or regulatory event is relevant to the EV "
        "battery industry / critical minerals supply chain.  Use is_relevant "
        "= true when the text discusses: lithium-ion batteries, EV / electric "
        "vehicles, battery materials (Li/Co/Ni/Mn/graphite/phosphate/Cu/Al/"
        "REE/etc.), mining-refining-processing of critical minerals, battery "
        "recycling, energy storage, or supply-chain policy affecting any of "
        "these.  Use is_relevant = false for events about unrelated industries "
        "(agriculture, semiconductors-only, generic forced-labor laws, "
        "historical/colonial decrees, biofuels, generic environmental rules "
        "without mineral framing)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_relevant": {
                "type": "boolean",
                "description": "True iff the policy is in scope for EV-battery / critical-minerals analysis.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in the verdict on [0, 1]",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "reason": {
                "type": "string",
                "description": (
                    "Brief (≤200 chars) phrase explaining the verdict — name "
                    "the specific battery/CRM topic OR the off-scope subject."
                ),
            },
        },
        "required": ["is_relevant", "confidence"],
    },
}


_RELEVANCE_SYSTEM_PROMPT = (
    "You are a domain expert on the EV battery supply chain.  Your job is "
    "to decide whether a policy or regulatory event is in scope for a "
    "battery-industry critical-minerals analysis.  Return your verdict via "
    "the assess_battery_industry_relevance tool.  Bias toward 'is_relevant "
    "= true' when the text plausibly affects critical minerals, battery "
    "materials, mining/refining/recycling, or EV supply chains, even if no "
    "specific mineral is named; bias toward 'is_relevant = false' for "
    "policies clearly about other domains (agriculture, semiconductors "
    "alone, forced-labor due-diligence laws not focused on minerals, etc.)."
)


_TOOL_SCHEMA = {
    "name": "classify_materials",
    "description": (
        "Classify, for each candidate material, whether the source text is "
        "materially about that material (vs incidentally mentioning it). "
        "Return one entry per candidate.  Materials are 'materially about' "
        "when the text discusses supply, demand, pricing, regulation, "
        "operations, contracts, or specific business risk for them; OR when "
        "the text itself is a policy that DESIGNATES, LISTS, OR CLASSIFIES "
        "the material as critical, strategic, or otherwise prioritised by a "
        "government (e.g., 'Critical Minerals List', 'Strategic Minerals "
        "Designation', 'National Mineral Inventory', or any sovereign act "
        "naming a set of materials for prioritised treatment) — the listing "
        "itself IS the regulatory event for each named material and should "
        "count as 'materially about' even when no further per-mineral "
        "discussion follows.  'Incidentally mentioned' applies to materials "
        "that appear in passing within a non-targeting context — generic "
        "critical-minerals rhetoric in an unrelated policy, a multi-material "
        "risk-factor sentence in a 10-K filing, or as one of many examples "
        "in a paragraph that isn't itself a designation list."
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
                                "True if the text discusses this material "
                                "substantively, OR if the text is a policy "
                                "that designates / lists / classifies this "
                                "material as critical or strategic.  False "
                                "only when the material appears as a passing "
                                "mention in a non-targeting context."
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
            # 2026-05-12 fix: load .env BEFORE the env-var check.  The
            # previous code only ran load_dotenv() inside _call_haiku() /
            # _call_haiku_relevance(), which meant a user with the key in
            # .env (but not exported to the shell process) saw _enabled =
            # False at construction time — the API-call sites' load_dotenv
            # never fired because the classifier had already short-
            # circuited.  Load here so the env-var check sees .env-only
            # keys.  load_dotenv is idempotent + cheap so repeated calls
            # are harmless.
            try:
                from dotenv import load_dotenv  # type: ignore
                load_dotenv()
            except ImportError:
                pass
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

    def assess_battery_industry_relevance(
        self,
        text: str,
        policy_type_names: Optional[list[str]] = None,
    ) -> tuple[bool, float, str]:
        """Ask Haiku whether ``text`` is in scope for EV-battery / critical-
        minerals analysis.

        Used as a final gate for events where keyword scan + tech basket
        produced zero candidate materials.  The IEA Policy Tracker calls this
        when it's about to write an event with ``needs_material_review=true``
        — events that fail this gate are dropped as off-scope rather than
        polluting the review queue with agriculture-circular-economy,
        forced-labor-due-diligence, or non-battery-mining noise.

        Args:
            text:                title + description of the event, truncated
                                 to ``_MAX_TEXT_CHARS`` inside this method.
            policy_type_names:   optional list of policy-type strings (e.g.
                                 ``["Strategic plans", "Financing"]``).
                                 Forwarded to Haiku as additional context.

        Returns:
            ``(is_relevant, confidence, reason)``.  When the classifier is
            disabled (no ``ANTHROPIC_API_KEY``), text too thin, or the Haiku
            call fails for any reason, returns ``(True, 0.5, "haiku_unavailable")``
            — the conservative default is to PRESERVE the event so the
            analyst can decide.  Callers can compare ``confidence`` against
            a threshold (e.g. only drop when ``not is_relevant and conf >=
            0.5``) but the simpler usage is to trust the boolean.
        """
        if not self._enabled:
            return (True, 0.5, "haiku_unavailable")
        if not text or len(text) < _MIN_TEXT_CHARS:
            return (True, 0.5, "text_too_short")

        # Cache the verdict per (text, policy_type_set) so we don't re-ask
        # Haiku within a single ingest run.
        policy_key = ",".join(sorted(policy_type_names or []))
        cache_key = _content_key(text[:_MAX_TEXT_CHARS], [policy_key])
        cached = self._relevance_cache.get(cache_key) if hasattr(self, "_relevance_cache") else None
        if cached is not None:
            return cached

        try:
            verdict = self._call_haiku_relevance(
                text[:_MAX_TEXT_CHARS], policy_type_names or []
            )
        except Exception:  # noqa: BLE001 — log + fall back, never crash ingest
            log.warning(
                "material_classifier.relevance_call_failed",
                policy_type_names=policy_type_names,
                exc_info=True,
            )
            return (True, 0.5, "haiku_call_failed")

        if not hasattr(self, "_relevance_cache"):
            self._relevance_cache: dict[str, tuple[bool, float, str]] = {}
        self._relevance_cache[cache_key] = verdict
        return verdict

    def _call_haiku_relevance(
        self,
        text: str,
        policy_type_names: list[str],
    ) -> tuple[bool, float, str]:
        """Make the Anthropic API call for the relevance assessment.

        Raises on any failure — caller catches.
        """
        if self._client is None:
            from anthropic import Anthropic  # type: ignore

            try:
                from dotenv import load_dotenv  # type: ignore
                load_dotenv()
            except ImportError:
                pass

            self._client = Anthropic(timeout=_REQUEST_TIMEOUT_S)

        policy_block = (
            "\n".join(f"  - {n}" for n in policy_type_names)
            if policy_type_names
            else "  (none provided)"
        )
        user_text = (
            "Source text:\n"
            "---\n"
            f"{text}\n"
            "---\n\n"
            "Policy type tags (curated by the source):\n"
            f"{policy_block}\n\n"
            "Call assess_battery_industry_relevance with your verdict."
        )

        response = self._client.messages.create(
            model=_MODEL,
            max_tokens=_MAX_OUTPUT_TOKENS,
            system=_RELEVANCE_SYSTEM_PROMPT,
            tools=[_RELEVANCE_TOOL_SCHEMA],
            tool_choice={
                "type": "tool",
                "name": "assess_battery_industry_relevance",
            },
            messages=[{"role": "user", "content": user_text}],
        )

        tool_block = next(
            (b for b in response.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if tool_block is None:
            raise RuntimeError(
                "Haiku response missing assess_battery_industry_relevance tool_use"
            )
        payload = tool_block.input
        if isinstance(payload, str):
            payload = json.loads(payload)

        is_rel = bool(payload.get("is_relevant"))
        conf = float(payload.get("confidence") or 0.0)
        conf = max(0.0, min(1.0, conf))
        reason = (payload.get("reason") or "")[:200]

        log.debug(
            "material_classifier.relevance_verdict",
            is_relevant=is_rel,
            confidence=conf,
            reason=reason,
        )
        return (is_rel, conf, reason)

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
            # match_reason DB columns are VARCHAR(64) across all junction
            # tables.  "llm_confirmed:" prefix eats 14 chars; that leaves
            # 50 for the suffix.  When a caller has already pre-prefixed
            # the keyword (IEA prepends "keyword_scan:" → up to 13 chars
            # before the actual keyword), the chain "llm_confirmed:keyword_scan:..."
            # is already at 27 chars, so we have ~37 chars left for keyword
            # + separator + evidence.  Tight budget — truncate aggressively
            # and cap the final string at 64 to honour the column.
            if evidence:
                kw_label = f"llm_confirmed:{matched_kw[:24]}|{evidence[:24]}"
            else:
                kw_label = f"llm_confirmed:{matched_kw[:48]}"
            refined.append((mid, new_relevance, kw_label[:64], hs_id))
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
            #   - is_material=False with high confidence (≥0.7) → 0.0 (drop)
            #   - is_material=False with lower confidence → keep at 0.5 (uncertain)
            #
            # 2026-05-12 calibration: rejection threshold raised from 0.5
            # to 0.7 after the first Haiku-enabled IEA run produced 47%
            # false-negative rejections — mainly sovereign critical-
            # minerals strategy documents (Morocco Mines Plan, Nigeria
            # Strategic Minerals List, Indonesia decree, etc.) where the
            # text designates materials as strategic without per-mineral
            # supply discussion.  Combined with the prompt refinement
            # (see _TOOL_SCHEMA description) that explicitly tells Haiku
            # to count designation/listing as 'materially about', the
            # threshold raise gives a safety net: even when Haiku's
            # specific-mineral discussion read is technically valid,
            # uncertain rejections (conf < 0.7) keep the attribution at
            # half-weight rather than dropping it.
            _HAIKU_REJECT_THRESHOLD = 0.7
            if is_mat:
                multiplier = conf
            elif conf >= _HAIKU_REJECT_THRESHOLD:
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
