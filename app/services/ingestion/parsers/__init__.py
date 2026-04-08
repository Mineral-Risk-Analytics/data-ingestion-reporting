"""Structured extraction from raw source payloads (Phase 1)."""

from app.services.ingestion.parsers.article_parser import ParsedArticle, parse_article
from app.services.ingestion.parsers.filing_parser import ParsedFiling, parse_sec_filing
from app.services.ingestion.parsers.regulation_parser import ParsedRegulation, parse_federal_register_document
from app.services.ingestion.parsers.tabular_parser import ParsedTradeRow, parse_census_trade_rows

__all__ = [
    "ParsedArticle",
    "ParsedFiling",
    "ParsedRegulation",
    "ParsedTradeRow",
    "parse_article",
    "parse_census_trade_rows",
    "parse_federal_register_document",
    "parse_sec_filing",
]
