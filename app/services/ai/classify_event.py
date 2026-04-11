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
            # Trade/tariff signals → geopolitical_trade pillar
            tags.append(RiskCategory.GEOPOLITICAL_TRADE.value)
        if any(k in blob for k in ("sec", "10-k", "8-k", "filing", "guidance")):
            # SEC filing signals → financial_pressure pillar
            tags.append(RiskCategory.FINANCIAL_PRESSURE.value)
        if any(
            k in blob
            for k in ("epa", "nhtsa", "federal register", "rule", "executive order")
        ):
            tags.append(RiskCategory.REGULATORY_COMPLIANCE.value)
        if any(k in blob for k in ("charging", "grid", "infrastructure")):
            # Infrastructure/grid context feeds country-concentration risk
            tags.append(RiskCategory.GEOPOLITICAL_TRADE.value)
        if not tags:
            tags.append(RiskCategory.OPERATIONAL.value)
        return list(dict.fromkeys(tags))
