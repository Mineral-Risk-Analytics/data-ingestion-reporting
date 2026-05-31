"""Background task hooks for scheduled scoring and ingestion jobs.

Imports both ``scoring_jobs`` and ``ingestion_jobs`` purely for the decorator
side-effects that register cron-triggered functions on ``inngest_client``.
``app/main.py`` relies on this import to populate ``ALL_SCHEDULED_FUNCTIONS``
before ``inngest.fast_api.serve`` is called.
"""

from app.tasks.ingestion_jobs import INGESTION_FUNCTIONS
from app.tasks.scoring_jobs import SCHEDULED_FUNCTIONS

ALL_SCHEDULED_FUNCTIONS = SCHEDULED_FUNCTIONS + INGESTION_FUNCTIONS

__all__ = ["ALL_SCHEDULED_FUNCTIONS", "SCHEDULED_FUNCTIONS", "INGESTION_FUNCTIONS"]
