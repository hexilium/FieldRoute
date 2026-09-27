"""Bounded, atomic two-job moves beyond single-visit relocation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class _BudgetExhausted(Exception):
    pass


class CompoundSearch:
    def __init__(self, search: Search, budget: SearchBudget) -> None:
        self.search = search
        self.budget = budget

    async def evaluate(self, eid: str, jobs: tuple[str, ...]) -> Schedule | None:
        if not self.budget.take():
            raise _BudgetExhausted
        route, _ = await self.search.evaluator.evaluate(eid, jobs)
        return route

    async def repairs(
        self, routes: dict[str, Schedule],
    ) -> AsyncIterator[dict[str, Schedule]]:
        """Insert a missing job and reinsert one displaced job; retain both."""
        search = self.search
        present = {jid for route in routes.values() for jid in route.jobs}
        missing = search.order([job for job in search.jobs if job.id not in present], "scarcity")
        for job in missing:
            search.check_time()
            for eid in search.ids:
                if search.evaluator.eligibility(job, search.evaluator.engineers[eid]):
                    continue
                source = routes[eid]
                for index, displaced in enumerate(source.jobs):
                    search.check_time()
                    reduced = source.jobs[:index] + source.jobs[index + 1:]
                    for position in range(len(reduced) + 1):
                        replacement = reduced[:position] + (job.id,) + reduced[position:]
                        route = await self.evaluate(eid, replacement)
                        if route is None:
                            continue
                        # Do not commit the intermediate plan, even if it has a
                        # better urgency score. The displaced job must be retained.
                        trial = {**routes, eid: route}
                        candidate = await search.insert(
                            trial, search.by_id[displaced], budget=self.budget,
                        )
                        if candidate is not None:
                            yield candidate
                        if self.budget.attempts >= self.budget.limit:
                            raise _BudgetExhausted

    async def swaps(
        self, routes: dict[str, Schedule],
    ) -> AsyncIterator[dict[str, Schedule]]:
        """Swap two visit positions, within one route or across engineers."""
        search = self.search
        for left_index, left_id in enumerate(search.ids):
            left = routes[left_id]
            for right_id in search.ids[left_index:]:
                right = routes[right_id]
                for i, left_job in enumerate(left.jobs):
                    search.check_time()
                    if search.evaluator.eligibility(
                        search.by_id[left_job], search.evaluator.engineers[right_id],
                    ):
                        continue
                    start = i + 1 if left_id == right_id else 0
                    for j in range(start, len(right.jobs)):
                        search.check_time()
                        right_job = right.jobs[j]
                        if search.evaluator.eligibility(
                            search.by_id[right_job], search.evaluator.engineers[left_id],
                        ):
                            continue
                        left_jobs = list(left.jobs)
                        left_jobs[i] = right_job
                        if left_id == right_id:
                            left_jobs[j] = left_job
                        changed_left = await self.evaluate(left_id, tuple(left_jobs))
                        if changed_left is None:
                            continue
                        if left_id == right_id:
                            yield {**routes, left_id: changed_left}
                            continue
                        right_jobs = list(right.jobs)
                        right_jobs[j] = left_job
                        changed_right = await self.evaluate(right_id, tuple(right_jobs))
                        if changed_right is not None:
                            yield {**routes, left_id: changed_left, right_id: changed_right}

    async def improve(self, routes: dict[str, Schedule]) -> tuple[dict[str, Schedule], dict]:
        initial_key = self.search.key(routes)
        key = initial_key
        moves = {"repair": 0, "swap": 0}
        passes = 0
        reason = "attempt_limit"
        try:
            while self.budget.attempts < self.budget.limit:
                passes += 1
                improved = False
                for kind, candidates in (("repair", self.repairs), ("swap", self.swaps)):
                    async for candidate in candidates(routes):
                        candidate_key = self.search.key(candidate)
                        if candidate_key < key:
                            routes, key = candidate, candidate_key
                            moves[kind] += 1
                            self.search.moves += 1
                            self.search.checkpoint(routes, kind)
                            improved = True
                            break
                    if improved:
                        break  # Restart from the new routes, including missing jobs.
                if not improved:
                    reason = "neighborhood_exhausted"
                    break
        except _BudgetExhausted:
            pass  # No partially checked swap or repair can reach routes.
        return routes, {
            "optimization_policy": self.search.request.optimization_policy.value,
            "stop_reason": reason,
            "passes": passes,
            "schedule_attempts": self.budget.attempts,
            "max_schedule_attempts": self.budget.limit,
            "accepted_moves": sum(moves.values()),
            "accepted_repairs": moves["repair"],
            "accepted_swaps": moves["swap"],
            "initial_objective_value": list(initial_key),
            "objective_value": list(key),
        }
