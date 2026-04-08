"""
Classify risk events into taxonomy dimensions.

Phase 1: keyword-based `StubEventClassifier`.
Future: LLM classifier with constrained JSON schema output.
"""

from __future__ import annotations

from typing import Protocol

from app.constants import RiskCategory


class EventClassifier(Protocol):
    def classify(self, title: str, summary: str | None) -> list[str]:
        ...


class StubEventClassifier:
    def classify(self, title: str, summary: str | None) -> list[str]:
        blob = f"{title} {summary or ''}".lower()
        tags: list[str] = []
        if any(k in blob for k in ("tariff", "import", "export", "section 232")):
            tags.append(RiskCategory.MATERIAL_SUPPLY.value)
        if any(k in blob for k in ("sec", "10-k", "8-k", "filing", "guidance")):
            tags.append(RiskCategory.SUPPLIER_OPERATIONAL.value)
        if any(
            k in blob
            for k in ("epa", "nhtsa", "federal register", "rule", "executive order")
        ):
            tags.append(RiskCategory.REGULATORY_POLICY.value)
        if any(k in blob for k in ("charging", "grid", "infrastructure")):
            tags.append(RiskCategory.INFRASTRUCTURE_ECOSYSTEM.value)
        if not tags:
            tags.append(RiskCategory.MARKET_DEMAND.value)
        return list(dict.fromkeys(tags))
