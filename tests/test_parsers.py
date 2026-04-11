"""Parser unit tests (Phase 1)."""

from app.services.ingestion.parsers.article_parser import parse_article
from app.services.ingestion.parsers.filing_parser import parse_sec_filing
from app.services.ingestion.parsers.regulation_parser import parse_federal_register_document
from app.services.ingestion.parsers.tabular_parser import parse_census_trade_rows


def test_parse_federal_register_document() -> None:
    doc = {
        "document_number": "2024-12345",
        "title": "Critical minerals procurement",
        "abstract": "Battery materials sourcing pilot.",
        "publication_date": "2024-05-01",
        "effective_on": "2024-07-01",
        "type": "Notice",
        "html_url": "https://www.federalregister.gov/documents/2024/05/01/2024-12345/test",
        "agencies": [{"name": "Department of Example"}],
        "topics": [{"name": "Energy"}],
    }
    parsed = parse_federal_register_document(doc)
    assert parsed.external_id == "2024-12345"
    assert parsed.title == "Critical minerals procurement"
    assert parsed.publication_date is not None
    assert parsed.effective_date is not None
    assert "Department of Example" in parsed.agencies


def test_parse_census_trade_rows() -> None:
    table = [
        ["CTY_CODE", "CTY_NAME", "I_COMMODITY", "I_COMMODITY_LDESC", "GEN_VAL_MO", "time"],
        ["5700", "China", "850760", "Lithium-ion batteries", "1000", "2024-11"],
    ]
    rows = parse_census_trade_rows(table, import_export="import")
    assert len(rows) == 1
    assert rows[0].partner_code == "5700"
    assert rows[0].hs_code == "850760"
    assert rows[0].trade_value_usd == 1000.0


def test_parse_article() -> None:
    art = parse_article(
        {
            "id": "n1",
            "title": "Grid strain and fast charging",
            "source": "demo",
            "published_at": "2024-01-15T10:00:00+00:00",
            "body": "Utilities flagged winter peak loads.",
            "event_classification": ["geopolitical_trade"],
        }
    )
    assert art.external_id == "n1"
    assert "geopolitical_trade" in art.event_labels


def test_parse_sec_filing_recent() -> None:
    submissions = {
        "cik": "1318605",
        "name": "Tesla, Inc.",
        "tickers": ["TSLA"],
        "filings": {
            "recent": {
                "form": ["10-K", "8-K"],
                "accessionNumber": ["0001-24-000001", "0001-24-000002"],
                "filingDate": ["2024-01-30", "2024-02-05"],
                "primaryDocument": ["primary.htm", "primary.htm"],
            }
        },
    }
    filings = parse_sec_filing(submissions, max_filings=2)
    assert len(filings) == 2
    assert filings[0].form == "10-K"
    assert filings[0].accession_number == "0001-24-000001"
    assert "Tesla" in (filings[0].company_name or "")
