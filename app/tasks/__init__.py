"""Background task hooks for scheduled scoring jobs.

Imports ``scoring_jobs`` purely for the decorator side-effects that register
the cron-triggered functions on ``inngest_client``. ``app/main.py`` relies
on this import to populate ``SCHEDULED_FUNCTIONS`` before ``inngest.fast_api
.serve`` is called.
"""

from app.tasks.scoring_jobs import SCHEDULED_FUNCTIONS

__all__ = ["SCHEDULED_FUNCTIONS"]
