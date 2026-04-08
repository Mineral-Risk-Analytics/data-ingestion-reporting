"""Normalizer tests."""

from unittest.mock import MagicMock

from app.services.ingestion.normalizers.geography_resolver import GeographyResolver
from app.services.ingestion.normalizers.supplier_resolver import SupplierResolver


def test_geography_resolver_maps_census_code() -> None:
    g = GeographyResolver()
    assert g.partner_country_iso2("5700") == "CN"
    assert g.region_for_country("US") == "north_america"


def test_supplier_resolver_finds_alias() -> None:
    supplier = MagicMock()
    supplier.canonical_name = "Tesla Inc."
    alias_row = MagicMock()
    alias_row.supplier = supplier

    mock_db = MagicMock()

    def exec_side_effect(*_a, **_kw):
        m = MagicMock()
        m.scalar_one_or_none.return_value = alias_row
        return m

    mock_db.execute.side_effect = exec_side_effect

    r = SupplierResolver(mock_db)
    assert r.resolve_name("Tesla Motors Inc") is supplier
