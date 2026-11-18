"""Smoke tests for the SEC filing body section parser.

Targets the regex fallback path explicitly — edgartools is exercised in
its own integration test once we land a fetcher that pulls real filings.
These tests use synthetic HTML modeled on actual 10-K and 20-F filing
shapes (TOC + body + section headers in tables / paragraphs / bold tags).
"""

import pytest

from app.services.ingestion.parsers.filing_body_parser import (
    SectionParseResult,
    parse_filing_sections,
    target_sections_for_form,
)


# ---------------------------------------------------------------------------
# Fixture HTML — mimics real 10-K formatting variation
# ---------------------------------------------------------------------------

# Modeled on a typical mining-issuer 10-K (Albemarle / MP Materials shape).
# Includes a TOC that mentions every Item, then a body where each Item is
# repeated as a bolded section header.  The regex fallback should pick the
# LATER occurrence (body, not TOC).
_SYNTHETIC_10K = """
<html>
  <head><title>FORM 10-K</title></head>
  <body>
    <h1>FORM 10-K</h1>
    <h2>Table of Contents</h2>
    <table>
      <tr><td>Item 1A. Risk Factors</td><td>12</td></tr>
      <tr><td>Item 2. Properties</td><td>34</td></tr>
      <tr><td>Item 7. Management's Discussion and Analysis</td><td>56</td></tr>
    </table>

    <h2>Part I</h2>
    <p>...</p>

    <h2><b>Item 1A. Risk Factors</b></h2>
    <p>Our business depends on the continued operation of lithium hydroxide
    facilities in Western Australia, Chile, and the United States. A
    disruption at any single site could materially affect our results.
    The lithium market remains concentrated and dependent on a small
    number of producers. Geopolitical tensions involving China have
    historically introduced significant volatility into pricing.</p>
    <p>Additional risk factor text would normally span many pages here.
    For the purposes of this test fixture we keep it short while still
    exceeding the minimum-section-length threshold for the parser to
    treat it as a real extraction rather than a TOC-only match.</p>

    <h2><b>Item 1B. Unresolved Staff Comments</b></h2>
    <p>None.</p>

    <h2><b>Item 2. Properties</b></h2>
    <p>The Company operates the following principal facilities:</p>
    <table>
      <tr><th>Facility</th><th>Country</th><th>Stage</th><th>Capacity (t/yr)</th></tr>
      <tr><td>Kemerton lithium hydroxide plant</td><td>Australia</td><td>refined</td><td>50,000</td></tr>
      <tr><td>La Negra carbonate plant</td><td>Chile</td><td>refined</td><td>40,000</td></tr>
      <tr><td>Silver Peak brine operation</td><td>United States</td><td>concentrate</td><td>5,000</td></tr>
    </table>
    <p>Each facility is subject to local environmental regulations and
    permits, and we periodically evaluate capacity expansion projects
    against medium-term demand forecasts.</p>

    <h2><b>Item 3. Legal Proceedings</b></h2>
    <p>None material.</p>

    <h2><b>Item 7. Management's Discussion and Analysis</b></h2>
    <p>Revenue for the fiscal year was driven primarily by lithium
    hydroxide sales to cathode-precursor customers in Asia and Europe.
    Cost of goods sold rose due to higher spodumene input prices and
    energy costs at our refining facilities. We continue to invest in
    expansion of our integrated lithium platform across upstream and
    downstream stages of the supply chain.</p>

    <h2><b>Item 7A. Quantitative and Qualitative Disclosures</b></h2>
    <p>Market risk text...</p>

    <h2><b>Item 8. Financial Statements</b></h2>
    <p>See financial statements attached.</p>
  </body>
</html>
"""


_SYNTHETIC_20F = """
<html>
  <body>
    <h1>FORM 20-F</h1>
    <h2>Item 3. Key Information</h2>
    <h3>Item 3.D Risk Factors</h3>
    <p>We are exposed to fluctuations in iron ore prices, which are
    influenced by Chinese steel demand and macroeconomic conditions.
    Our principal iron ore mines are located in Brazil and Australia.
    Operational disruptions at these sites would materially affect our
    consolidated results.</p>
    <p>Additional risk-factor paragraphs exceed the minimum threshold
    that the parser uses to distinguish real section content from a
    table-of-contents-only match.</p>

    <h2>Item 4. Information on the Company</h2>
    <h3>Item 4.D Property, Plants and Equipment</h3>
    <p>Our principal mining and processing facilities are:</p>
    <ul>
      <li>Carajas iron ore mine — Para, Brazil — operating</li>
      <li>Itabira mining complex — Minas Gerais, Brazil — operating</li>
      <li>Sudbury nickel operations — Ontario, Canada — operating</li>
      <li>Onca Puma nickel-cobalt project — Para, Brazil — care &amp; maintenance</li>
    </ul>

    <h2>Item 5. Operating and Financial Review and Prospects</h2>
    <p>Fiscal-year revenue was driven primarily by iron ore exports to
    Asia. Cost inflation and currency volatility impacted reported
    margins. Free cash flow generation funded sustaining capital and
    shareholder returns.</p>

    <h2>Item 6. Directors, Senior Management and Employees</h2>
    <p>...</p>
  </body>
</html>
"""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class Test10KExtraction:
    """End-to-end on the synthetic 10-K fixture."""

    @pytest.fixture
    def results(self) -> list[SectionParseResult]:
        return parse_filing_sections(
            raw_html=_SYNTHETIC_10K, form="10-K",
            accession_number="0000000000-00-000000",
        )

    def test_all_three_sections_found(self, results):
        codes = {r.section_code for r in results}
        assert codes == {"10-K.item_1a", "10-K.item_2", "10-K.item_7"}

    def test_risk_factors_contains_material_mentions(self, results):
        item_1a = next(r for r in results if r.section_code == "10-K.item_1a")
        assert "lithium" in item_1a.text.lower()
        assert "china" in item_1a.text.lower()
        # The body text should be picked, not the 1-line TOC entry.
        assert item_1a.char_count > 200

    def test_properties_contains_facility_table_text(self, results):
        item_2 = next(r for r in results if r.section_code == "10-K.item_2")
        assert "kemerton" in item_2.text.lower()
        assert "la negra" in item_2.text.lower()
        assert "silver peak" in item_2.text.lower()
        # Properties body should NOT bleed into Item 3 Legal Proceedings.
        assert "legal proceedings" not in item_2.text.lower()

    def test_mda_terminates_before_item_7a(self, results):
        item_7 = next(r for r in results if r.section_code == "10-K.item_7")
        assert "revenue" in item_7.text.lower()
        # MD&A body should NOT include Item 7A's "Market risk text".
        assert "market risk text" not in item_7.text.lower()

    def test_each_result_has_regex_method_in_no_edgartools_env(self, results):
        # In a test env without edgartools, every result should be tagged
        # `regex`.  Once edgartools is installed and the lazy import
        # succeeds, this test will need to flex.
        for r in results:
            assert r.method in {"regex", "edgartools"}
            assert r.version
            assert r.char_count == len(r.text)


class Test20FExtraction:
    """20-F has its own item taxonomy (3.D / 4.D / 5)."""

    @pytest.fixture
    def results(self) -> list[SectionParseResult]:
        return parse_filing_sections(
            raw_html=_SYNTHETIC_20F, form="20-F",
            accession_number="0000000000-00-000001",
        )

    def test_all_three_20f_sections_found(self, results):
        codes = {r.section_code for r in results}
        assert codes == {"20-F.item_3d", "20-F.item_4d", "20-F.item_5"}

    def test_item_4d_contains_facility_list(self, results):
        item_4d = next(r for r in results if r.section_code == "20-F.item_4d")
        assert "carajas" in item_4d.text.lower()
        assert "sudbury" in item_4d.text.lower()


class TestFormSupport:
    def test_unknown_form_returns_empty(self):
        results = parse_filing_sections(
            raw_html=_SYNTHETIC_10K, form="6-K",
            accession_number="x",
        )
        assert results == []

    def test_supported_forms_have_target_sections(self):
        assert len(target_sections_for_form("10-K")) == 3
        assert len(target_sections_for_form("20-F")) == 3
        assert target_sections_for_form("8-K") == []


class TestEdgeCases:
    def test_empty_html_returns_empty(self):
        results = parse_filing_sections(
            raw_html="", form="10-K", accession_number="x",
        )
        assert results == []

    def test_very_short_html_returns_empty(self):
        # Below the 500-char threshold in _extract_via_regex.
        results = parse_filing_sections(
            raw_html="<html><body>tiny</body></html>",
            form="10-K", accession_number="x",
        )
        assert results == []

    def test_html_without_target_anchors_returns_empty(self):
        # Real HTML, real length, but no matching item anchors.
        body = "<html><body>" + ("<p>Lorem ipsum dolor sit amet.</p>" * 50) + "</body></html>"
        results = parse_filing_sections(
            raw_html=body, form="10-K", accession_number="x",
        )
        assert results == []
