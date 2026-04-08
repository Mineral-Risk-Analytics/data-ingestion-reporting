"""Provider-agnostic AI helpers (stubs now; swap implementations later)."""

from app.services.ai.classify_event import EventClassifier, StubEventClassifier
from app.services.ai.cluster_duplicates import DuplicateClusterer, StubDuplicateClusterer
from app.services.ai.summarize_document import DocumentSummarizer, StubDocumentSummarizer

__all__ = [
    "DocumentSummarizer",
    "DuplicateClusterer",
    "EventClassifier",
    "StubDocumentSummarizer",
    "StubDuplicateClusterer",
    "StubEventClassifier",
]
