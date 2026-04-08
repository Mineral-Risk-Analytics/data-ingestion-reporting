"""Parse U.S. Census international trade time-series tables into trade rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ParsedTradeRow:
    period: str
    reporter_country: str
    partner_code: str
    partner_name: str | None
    hs_code: str | None
    hs_description: str | None
    trade_value_usd: float | None
    quantity: float | None
    quantity_unit: str | None
    import_export: str
    extra: dict[str, Any] = field(default_factory=dict)


def _to_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_census_trade_rows(
    census_response: list[Any],
    *,
    import_export: str = "import",
) -> list[ParsedTradeRow]:
    """
    Census returns: [ [col, ...], [row0], [row1], ... ].
    """
    if not census_response or not isinstance(census_response, list):
        return []
    header = census_response[0]
    if not isinstance(header, list):
        return []
    idx = {str(h): i for i, h in enumerate(header)}
    rows: list[ParsedTradeRow] = []
    for raw in census_response[1:]:
        if not isinstance(raw, list):
            continue
        def col(name: str, default: str | None = None) -> Any:
            i = idx.get(name)
            if i is None or i >= len(raw):
                return default
            return raw[i]

        period = str(col("time", ""))
        cty_code = str(col("CTY_CODE", "") or col("CTY_CODE...", "") or "")
        cty_name = col("CTY_NAME")
        commodity = col("I_COMMODITY") or col("E_COMMODITY") or col("COMMODITY")
        desc = col("I_COMMODITY_LDESC") or col("E_COMMODITY_LDESC") or col("COMM_DESC")
        gen_val = _to_float(col("GEN_VAL_MO") or col("ALL_VAL_MO") or col("value"))
        qty = _to_float(col("QTY_1_MO") or col("quantity"))
        unit = col("UNIT_QY1")

        rows.append(
            ParsedTradeRow(
                period=period,
                reporter_country="US",
                partner_code=cty_code,
                partner_name=str(cty_name) if cty_name else None,
                hs_code=str(commodity) if commodity else None,
                hs_description=str(desc)[:512] if desc else None,
                trade_value_usd=gen_val,
                quantity=qty,
                quantity_unit=str(unit) if unit else None,
                import_export=import_export,
                extra={},
            )
        )
    return rows
