"""Request-local progress; a slow reader retains only the latest snapshot."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from time import perf_counter


class PlanningProgress:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=1)
        self.started = perf_counter()
        self.last_sent = 0.0
        self.state: dict = {"phase": "starting", "plan_number": 0}

    def snapshot(self) -> dict:
        return {"type": "progress", **self.state,
                "elapsed_ms": round((perf_counter() - self.started) * 1000)}

    def update(self, *, force: bool = False, **fields) -> None:
        self.state.update(fields)
        now = perf_counter()
        if not force and now - self.last_sent < 0.2:
            return
        self.last_sent = now
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(self.snapshot())

    def begin(self, total_jobs: int, solver: str) -> None:
        self.update(
            force=True, phase="routing", stage="", processed_jobs=0,
            district_id=None, district_number=0, district_count=0,
            total_jobs=total_jobs, best_assigned=0, schedule_attempts=0,
            search_strategy="full" if solver == "insertion" else solver,
            attempt_limit=None, budget_attempts=0, budget_remaining=None,
            completed_candidates=0, total_candidates=45 if solver == "insertion" else 1,
            plan_number=self.state["plan_number"] + 1,
        )

    def committed(self, routes) -> None:
        assigned = sum(len(route.visits) for route in routes.values())
        self.update(best_assigned=max(self.state.get("best_assigned", 0), assigned))


current_progress: ContextVar[PlanningProgress | None] = ContextVar("planning_progress", default=None)
