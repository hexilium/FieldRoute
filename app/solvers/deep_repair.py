"""A separately budgeted three-ejection repair neighborhood."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.solvers.repair import RepairSearch

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class DeepRepairSearch(RepairSearch):
    """Repair one missing job after displacing at most three assigned jobs.

    The historical v8/v9 stages intentionally remain capped at two ejections.
    This separate stage reuses their atomic depth-first repair and bounded
    alternative insertions without changing those older candidate semantics.
    """

    stage = "deep_repair"

    def __init__(
        self,
        search: Search,
        budget: SearchBudget,
        *,
        max_ejections: int = 3,
        max_job_attempts: int = 2000,
        insertion_width: int = 3,
    ) -> None:
        if (
            isinstance(budget.limit, bool)
            or not isinstance(budget.limit, int)
            or budget.limit < 0
        ):
            raise ValueError("schedule attempt limit must be a nonnegative integer")
        if (
            isinstance(max_ejections, bool)
            or not isinstance(max_ejections, int)
            or max_ejections != 3
        ):
            raise ValueError("max_ejections must be exactly 3")
        if (
            isinstance(max_job_attempts, bool)
            or not isinstance(max_job_attempts, int)
            or max_job_attempts < 1
        ):
            raise ValueError("max_job_attempts must be a positive integer")

        super().__init__(
            search,
            budget,
            insertion_width=insertion_width,
            max_ejections=max_ejections,
            max_job_attempts=max_job_attempts,
        )
