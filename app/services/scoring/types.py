"""Shared scoring DTOs."""

from dataclasses import dataclass, field


@dataclass
class ScoreResult:
    """Numeric score 0–100 plus human-readable rationale bullets."""

    score: float
    rationale: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.score = max(0.0, min(100.0, float(self.score)))
