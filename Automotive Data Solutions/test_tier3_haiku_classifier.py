"""End-to-end test for Tier 3 — Haiku-based material classifier (2026-05-09).

Verifies:

  T3.1  Classifier auto-disables when ANTHROPIC_API_KEY is absent;
        ``classify()`` is then a passthrough (returns input unchanged).

  T3.2  Cache key is stable across permutations of the same candidate set.

  T3.3  Mocked Haiku response: ``classify()`` applies decisions —
        confirmed material's relevance gets scaled by confidence;
        rejected material is dropped from the output.

  T3.4  In-process cache: a second call with identical (text, candidates)
        does NOT invoke the LLM again.

  T3.5  Failure-mode: when the LLM call raises, ``classify()`` returns
        the original keyword matches unchanged (graceful degrade — the
        ingest never gets worse than its pure-keyword baseline).

  T3.6  Edge cases: short text below the min-chars threshold and empty
        candidate set both short-circuit to passthrough.
"""

from __future__ import annotations

import sys

sys.path.insert(0, "/sessions/keen-wonderful-lamport/mnt/battery-data-intelligence-engine")


def main() -> int:
    failures: list[str] = []

    from app.services.ingestion.material_classifier import (
        MaterialClassifier,
        _content_key,
        _MIN_TEXT_CHARS,
    )

    print("=== Tier 3: Haiku material classifier ===\n")

    MATS = {1: "Lithium", 2: "Cobalt", 3: "Nickel"}

    # ── T3.1: Disabled mode passthrough ──────────────────────────────────
    print("Test T3.1: classifier disabled → passthrough")
    mc = MaterialClassifier(materials_by_id=MATS, enabled=False)
    if mc.enabled:
        failures.append("classifier.enabled True when constructed with enabled=False")
    matches = [(1, 0.9, "lithium", None), (2, 0.7, "cobalt", None)]
    out = mc.classify("Long enough text " * 50, matches)
    if out != matches:
        failures.append(f"disabled classifier mutated output: {out}")
    else:
        print(f"  [OK] disabled classifier returned input unchanged ({len(out)} matches)")

    # ── T3.2: Cache key stability ────────────────────────────────────────
    print("\nTest T3.2: cache key stable across candidate-name permutations")
    k1 = _content_key("the same text", ["Lithium", "Cobalt"])
    k2 = _content_key("the same text", ["Cobalt", "Lithium"])
    if k1 != k2:
        failures.append(f"cache key permutation-sensitive: {k1} vs {k2}")
    else:
        print(f"  [OK] cache key invariant under permutation: {k1}")

    # Different text → different key
    k3 = _content_key("DIFFERENT text", ["Lithium", "Cobalt"])
    if k1 == k3:
        failures.append("cache key collision on different text")
    else:
        print(f"  [OK] cache key changes on text change")

    # ── T3.3-T3.5: Mock the Haiku client and exercise the call paths ─────
    print("\nTest T3.3: mocked Haiku call applies decisions correctly")

    class _MockToolBlock:
        type = "tool_use"
        def __init__(self, payload):
            self.input = payload

    class _MockResponse:
        def __init__(self, payload):
            self.content = [_MockToolBlock(payload)]

    class _MockMessages:
        def __init__(self, payload):
            self._payload = payload
            self.call_count = 0
        def create(self, **kwargs):
            self.call_count += 1
            return _MockResponse(self._payload)

    class _MockClient:
        def __init__(self, payload):
            self.messages = _MockMessages(payload)

    # Build classifier with enabled=True bypassing API key check, then
    # swap in the mock client manually.
    mc_on = MaterialClassifier(materials_by_id=MATS, enabled=False)
    mc_on._enabled = True  # type: ignore[attr-defined]
    # Haiku says: Lithium is materially about (conf 0.9), Cobalt isn't (conf 0.8 → drop)
    mock = _MockClient({
        "classifications": [
            {"material": "Lithium", "is_material": True,  "confidence": 0.9, "evidence": "supply contract"},
            {"material": "Cobalt",  "is_material": False, "confidence": 0.8, "evidence": "list mention"},
        ]
    })
    mc_on._client = mock  # type: ignore[attr-defined]

    long_text = (
        "We entered into a multi-year lithium hydroxide supply contract with "
        "Albemarle Corporation.  Our risk factors mention cobalt among 47 critical "
        "materials we may be subject to supply chain disruption on, but no specific "
        "cobalt arrangements are in place."
    ) * 2
    matches = [
        (1, 0.85, "lithium", None),
        (2, 0.65, "cobalt", None),
    ]
    out = mc_on.classify(long_text, matches)

    # Expectations:
    #  - Lithium kept, relevance scaled by 0.9 → 0.85 * 0.9 = 0.765
    #  - Cobalt rejected (is_material=False, conf=0.8 ≥ 0.5) → dropped
    if len(out) != 1:
        failures.append(f"expected 1 result after refinement, got {len(out)}: {out}")
    else:
        mid, score, kw, hs_id = out[0]
        if mid != 1:
            failures.append(f"kept material_id={mid}, expected 1 (Lithium)")
        elif abs(score - 0.765) > 0.01:
            failures.append(f"Lithium relevance = {score}, expected ~0.765")
        elif "llm_confirmed" not in kw:
            failures.append(f"matched_keyword lacks 'llm_confirmed' tag: {kw!r}")
        else:
            print(f"  [OK] Lithium kept (relevance={score:.3f}, label={kw!r})")
            print(f"  [OK] Cobalt rejected (Haiku said is_material=False)")

    # ── T3.4: Cache prevents repeated LLM calls ──────────────────────────
    print("\nTest T3.4: cache prevents repeated LLM calls for same input")
    out2 = mc_on.classify(long_text, matches)
    if mock.messages.call_count != 1:
        failures.append(
            f"LLM called {mock.messages.call_count} times, expected 1 (cache miss)"
        )
    elif out2 != out:
        failures.append(f"cached result differs from first call: {out2} vs {out}")
    else:
        print(f"  [OK] second call hit cache; LLM invoked {mock.messages.call_count} time(s) total")

    # ── T3.5: LLM error → graceful fallback ──────────────────────────────
    print("\nTest T3.5: LLM exception → return keyword matches unchanged")

    class _FailingMessages:
        def create(self, **kwargs):
            raise RuntimeError("simulated network error")

    class _FailingClient:
        messages = _FailingMessages()

    mc_fail = MaterialClassifier(materials_by_id=MATS, enabled=False)
    mc_fail._enabled = True  # type: ignore[attr-defined]
    mc_fail._client = _FailingClient()  # type: ignore[attr-defined]
    # Use different text so we miss any cache from above
    fallback_text = "Long enough fallback text " * 20
    out_fail = mc_fail.classify(fallback_text, matches)
    if out_fail != matches:
        failures.append(
            f"failing LLM should return input unchanged; got {out_fail}"
        )
    else:
        print(f"  [OK] LLM failure → returned {len(out_fail)} keyword matches unchanged")

    # ── T3.6: Short text and empty candidates short-circuit ──────────────
    print("\nTest T3.6: short text / empty candidates short-circuit")
    mc_short = MaterialClassifier(materials_by_id=MATS, enabled=False)
    mc_short._enabled = True  # type: ignore[attr-defined]
    # Even with no real client set, short text should not invoke it
    mc_short._client = None  # type: ignore[attr-defined]
    short_text = "tiny"
    out_short = mc_short.classify(short_text, matches)
    if out_short != matches:
        failures.append(f"short-text path returned {out_short}, expected passthrough")
    else:
        print(f"  [OK] short text ({len(short_text)} chars < {_MIN_TEXT_CHARS}) → passthrough")

    out_empty = mc_short.classify("anything long enough " * 50, [])
    if out_empty != []:
        failures.append(f"empty-candidate path returned {out_empty}")
    else:
        print(f"  [OK] empty candidate set → passthrough")

    print("\n" + "=" * 64)
    if failures:
        print("FAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS — Tier 3 Haiku material classifier:")
    print("  ✓ T3.1 Disabled mode passes through unchanged")
    print("  ✓ T3.2 Cache key stable across permutations / changes on text")
    print("  ✓ T3.3 Decisions applied: confirmed scaled, rejected dropped, label tagged")
    print("  ✓ T3.4 In-process cache prevents repeated LLM calls")
    print("  ✓ T3.5 LLM exception → graceful fallback to keyword matches")
    print("  ✓ T3.6 Short text + empty candidates short-circuit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
