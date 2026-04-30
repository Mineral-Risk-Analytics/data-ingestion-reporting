"""Phase 2 and Phase 3 source adapter stubs.

These adapters are registered in ``ADAPTER_BY_TYPE`` (parent ``__init__.py``)
so the pipeline can route to them by source_type, but they all raise
``NotImplementedError`` on fetch(). They are grouped here to keep the live
adapters (``census_trade``, ``federal_register``, ``news``, ``sec_edgar``) in
the top-level ``adapters/`` package and the placeholder work separate.

To activate an adapter:
1. Implement ``fetch()`` in the relevant module.
2. Set ``is_active = True`` on the corresponding ``Source`` row (or update
   ``seed_if_empty`` in ``app/db/seed.py``).
3. Move the file back to the parent ``adapters/`` package (optional — the
   import path works either way).
"""

from app.services.ingestion.adapters.future.canada_policy import CanadaPolicyAdapter
from app.services.ingestion.adapters.future.ngo_reports import NgoReportsAdapter
from app.services.ingestion.adapters.future.nrel_charging import NrelChargingAdapter
from app.services.ingestion.adapters.future.policy_pages import PolicyPagesAdapter
from app.services.ingestion.adapters.future.sustainability_reports import SustainabilityReportsAdapter
from app.services.ingestion.adapters.future.usitc import UsitcAdapter

__all__ = [
    "CanadaPolicyAdapter",
    "NgoReportsAdapter",
    "NrelChargingAdapter",
    "PolicyPagesAdapter",
    "SustainabilityReportsAdapter",
    "UsitcAdapter",
]
