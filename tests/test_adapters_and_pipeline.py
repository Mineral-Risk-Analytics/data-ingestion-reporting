"""Adapter tests with stub HTTP; pipeline logic covered via parser + normalizer integration."""

from app.services.ingestion.adapters.federal_register import FederalRegisterAdapter
from app.services.ingestion.normalizers.event_normalizer import build_regulatory_risk_event
from app.services.ingestion.parsers.regulation_parser import parse_federal_register_document


def test_federal_register_adapter_with_fake_client() -> None:
    adapter = FederalRegisterAdapter()

    class FakeResp:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict:
            return {
                "results": [
                    {
                        "document_number": "x-2",
                        "title": "Demo",
                        "publication_date": "2024-02-01",
                        "abstract": "lithium",
                        "agencies": [{"name": "EPA"}],
                        "topics": [],
                    }
                ]
            }

        @property
        def content(self) -> bytes:
            return b"{}"

    class FakeClient:
        def get(self, *_a, **_kw):
            return FakeResp()

    bundles = adapter.fetch(FakeClient(), params={"per_page": 3})
    assert bundles[0].items[0]["title"] == "Demo"


def test_regulation_to_risk_event_integration() -> None:
    """Lightweight substitute for full DB pipeline: parser + normalizer contract."""
    parsed = parse_federal_register_document(
        {
            "document_number": "2024-test",
            "title": "Notice",
            "publication_date": "2024-06-01",
            "abstract": "battery tariff supply chain",
            "agencies": [{"name": "DOE"}],
            "topics": [{"name": "Energy"}],
            "type": "Notice",
        }
    )
    draft = build_regulatory_risk_event(parsed)
    assert draft.event_type == "federal_register_notice"
    # severity_score is on the 0-1.0 scale (scoring v2); "battery tariff supply chain"
    # contributes multiple keyword hits pushing well above the 0.35 base.
    assert 0.0 < draft.severity_score <= 1.0
    assert draft.severity_score > 0.30
