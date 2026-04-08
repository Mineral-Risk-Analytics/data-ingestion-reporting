"""
Assemble OEM-facing narrative reports from `report_runs` + `report_insights`.

Phase 1: stub — define function signatures and docstrings for Phase 2+ SQL-backed assembly.
"""


def build_oem_report(*, report_run_id: int) -> None:
    # TODO: query insights, order by sort_order, render PDF/HTML export
    raise NotImplementedError("OEM report builder not implemented in Phase 1")
