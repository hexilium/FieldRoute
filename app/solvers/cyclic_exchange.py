"""Bounded atomic one-job cycles across three routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class _BudgetExhausted(Exception):
    pass


class _ProbeExhausted(Exception):
    pass


class CyclicExchangeSearch:
    """Rotate one future visit between three routes as one complete move.

    Pair exchanges cannot cross a plateau where every intermediate swap is
    infeasible or worse. A three-cycle evaluates both orientations without ever
    exposing one or two changed routes as a checkpoint.
    """

    stage = "cyclic_exchange"

    def __init__(
        self,
        search: Search,
        budget: SearchBudget,
        *,
        max_probes: int = 100_000,
    ) -> None:
        if (
            isinstance(budget.limit, bool)
            or not isinstance(budget.limit, int)
            or budget.limit < 0
        ):
            raise ValueError("schedule attempt limit must be a nonnegative integer")
        if (
            isinstance(max_probes, bool)
            or not isinstance(max_probes, int)
            or max_probes < 0
        ):
            raise ValueError("probe limit must be a nonnegative integer")
        self.search, self.budget = search, budget
        self.max_probes = max_probes
        self.probes = 0

    async def check_probe(self) -> None:
        """Bound filter-only work and keep deadline/cancellation responsive."""
        self.search.check_time()
        if self.probes >= self.max_probes:
            raise _ProbeExhausted
        self.probes += 1
        if self.probes % 128 == 0:
            if self.search.progress is not None:
                self.search.progress.update()
            await asyncio.sleep(0)

    async def evaluate(self, engineer_id: str, jobs: tuple[str, ...]) -> Schedule | None:
        self.search.check_time()
        if not self.budget.take():
            raise _BudgetExhausted
        route, _ = await self.search.evaluator.evaluate(engineer_id, jobs)
        return route

    def eligible(self, job_id: str, engineer_id: str) -> bool:
        evaluator = self.search.evaluator
        return not evaluator.eligibility(
            self.search.by_id[job_id], evaluator.engineers[engineer_id],
        )

    async def cycle(
        self,
        routes: dict[str, Schedule],
        engineer_ids: tuple[str, str, str],
        positions: tuple[int, int, int],
        job_ids: tuple[str, str, str],
        *,
        orientation: str,
    ) -> tuple[dict[str, Schedule], dict[str, Any]] | None:
        """Evaluate all three replacement routes before returning a candidate."""
        left_job, middle_job, right_job = job_ids
        if orientation == "forward":
            incoming = middle_job, right_job, left_job
        else:
            incoming = right_job, left_job, middle_job
        if not all(
            self.eligible(job_id, engineer_id)
            for job_id, engineer_id in zip(incoming, engineer_ids, strict=True)
        ):
            return None

        changed: dict[str, Schedule] = {}
        for engineer_id, position, job_id in zip(
            engineer_ids, positions, incoming, strict=True,
        ):
            old = routes[engineer_id]
            jobs = old.jobs[:position] + (job_id,) + old.jobs[position + 1:]
            route = await self.evaluate(engineer_id, jobs)
            if route is None:
                return None
            changed[engineer_id] = route

        candidate = {**routes, **changed}
        return candidate, {
            "orientation": orientation,
            "engineers": list(engineer_ids),
            "positions": list(positions),
            "outgoing_job_ids": list(job_ids),
            "incoming_job_ids": list(incoming),
        }

    async def candidates(
        self, routes: dict[str, Schedule],
    ) -> AsyncIterator[tuple[dict[str, Schedule], dict[str, Any]]]:
        search = self.search
        assigned = set().union(*(route.assigned_job_ids for route in routes.values()))
        urgency_sweeps = (True, False) if assigned & search._urgent_ids else (False,)
        for urgent in urgency_sweeps:
            for left_index, left_id in enumerate(search.ids):
                left = routes[left_id]
                if not left.jobs:
                    continue
                for middle_index in range(left_index + 1, len(search.ids)):
                    middle_id = search.ids[middle_index]
                    middle = routes[middle_id]
                    if not middle.jobs:
                        continue
                    for right_id in search.ids[middle_index + 1:]:
                        search.check_time()
                        right = routes[right_id]
                        if not right.jobs:
                            continue
                        if urgent and not (
                            (left.assigned_job_ids
                             | middle.assigned_job_ids
                             | right.assigned_job_ids)
                            & search._urgent_ids
                        ):
                            continue
                        engineer_ids = left_id, middle_id, right_id
                        for left_pos, left_job in enumerate(left.jobs):
                            for middle_pos, middle_job in enumerate(middle.jobs):
                                for right_pos, right_job in enumerate(right.jobs):
                                    await self.check_probe()
                                    job_ids = left_job, middle_job, right_job
                                    has_urgent = any(
                                        job_id in search._urgent_ids for job_id in job_ids
                                    )
                                    if has_urgent != urgent:
                                        continue
                                    positions = left_pos, middle_pos, right_pos
                                    for orientation in ("forward", "reverse"):
                                        result = await self.cycle(
                                            routes,
                                            engineer_ids,
                                            positions,
                                            job_ids,
                                            orientation=orientation,
                                        )
                                        if result is not None:
                                            yield result

    async def improve(
        self, routes: dict[str, Schedule],
    ) -> tuple[dict[str, Schedule], dict[str, Any]]:
        search = self.search
        initial_key = key = search.key(routes)
        moves: list[dict[str, Any]] = []
        passes = 0
        reason = "attempt_limit"
        try:
            while self.budget.attempts < self.budget.limit:
                passes += 1
                improved = False
                async for candidate, move in self.candidates(routes):
                    candidate_key = search.key(candidate)
                    if candidate_key < key:
                        routes, key = candidate, candidate_key
                        moves.append(move)
                        search.moves += 1
                        search.checkpoint(routes, self.stage)
                        improved = True
                        break
                if not improved:
                    reason = "neighborhood_exhausted"
                    break
        except _BudgetExhausted:
            reason = "attempt_limit"
        except _ProbeExhausted:
            reason = "probe_limit"
        return routes, {
            "optimization_policy": search.request.optimization_policy.value,
            "urgent_start_policy": search.request.urgent_start_policy.value,
            "objective_order": search.objective_order(),
            "stop_reason": reason,
            "passes": passes,
            "schedule_attempts": self.budget.attempts,
            "max_schedule_attempts": self.budget.limit,
            "probes": self.probes,
            "max_probes": self.max_probes,
            "accepted_moves": len(moves),
            "accepted_job_exchanges": 3 * len(moves),
            "forward_moves": sum(move["orientation"] == "forward" for move in moves),
            "reverse_moves": sum(move["orientation"] == "reverse" for move in moves),
            "moves": moves,
            "initial_objective_value": list(initial_key),
            "objective_value": list(key),
        }
