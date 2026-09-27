"""Bounded route reversals and atomic exchanges of two route tails."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class _BudgetExhausted(Exception):
    pass


class RouteSegmentSearch:
    """Move several visits together without dropping any assigned work.

    Reversals (2-opt) change the order within a route. Tail exchanges (2-opt*)
    keep each tail's order but change its engineer. Both use full directed
    schedule evaluation, including each receiving engineer's travel profile;
    a distance-only edge delta cannot establish feasibility or improvement.
    """

    stage = "segments"

    def __init__(self, search: Search, budget: SearchBudget) -> None:
        if budget.limit < 0:
            raise ValueError("max schedule attempts must be nonnegative")
        self.search, self.budget = search, budget

    async def evaluate(self, eid: str, jobs: tuple[str, ...]) -> Schedule | None:
        self.search.check_time()
        if not self.budget.take():
            raise _BudgetExhausted
        route, _ = await self.search.evaluator.evaluate(eid, jobs)
        return route

    async def reversals(
        self, routes: dict[str, Schedule],
    ) -> AsyncIterator[dict[str, Schedule]]:
        for eid in self.search.ids:
            source = routes[eid].jobs
            # Executing work is absent from jobs and remains the fixed prefix
            # installed by ScheduleEvaluator, even when the whole tail reverses.
            for start in range(len(source) - 1):
                for end in range(start + 2, len(source) + 1):
                    jobs = source[:start] + source[start:end][::-1] + source[end:]
                    route = await self.evaluate(eid, jobs)
                    if route is not None:
                        yield {**routes, eid: route}

    def first_eligible_cut(self, source: tuple[str, ...], target: str) -> int:
        """Every job after a cut must be allowed on the receiving engineer."""
        evaluator = self.search.evaluator
        for index in range(len(source) - 1, -1, -1):
            self.search.check_time()
            if evaluator.eligibility(self.search.by_id[source[index]], evaluator.engineers[target]):
                return index + 1
        return 0

    async def tail_exchanges(
        self, routes: dict[str, Schedule],
    ) -> AsyncIterator[dict[str, Schedule]]:
        search = self.search
        for index, left_id in enumerate(search.ids):
            left = routes[left_id].jobs
            for right_id in search.ids[index + 1:]:
                search.check_time()
                right = routes[right_id].jobs
                first_left = self.first_eligible_cut(left, right_id)
                first_right = self.first_eligible_cut(right, left_id)
                for left_cut in range(first_left, len(left) + 1):
                    for right_cut in range(first_right, len(right) + 1):
                        if left_cut == len(left) and right_cut == len(right):
                            continue  # Exchanging two empty tails changes nothing.
                        new_left = await self.evaluate(left_id, left[:left_cut] + right[right_cut:])
                        if new_left is None:
                            continue
                        new_right = await self.evaluate(right_id, right[:right_cut] + left[left_cut:])
                        if new_right is not None:
                            # Never expose one checked half of an exchange: it
                            # may duplicate some jobs while omitting others.
                            yield {**routes, left_id: new_left, right_id: new_right}

    async def improve(self, routes: dict[str, Schedule]) -> tuple[dict[str, Schedule], dict]:
        search = self.search
        initial_key = key = search.key(routes)
        moves = {"reversal": 0, "tail_exchange": 0}
        passes = 0
        reason = "attempt_limit"
        try:
            while self.budget.attempts < self.budget.limit:
                search.check_time()
                passes += 1
                improved = False
                for kind, candidates in (("reversal", self.reversals),
                                         ("tail_exchange", self.tail_exchanges)):
                    async for candidate in candidates(routes):
                        candidate_key = search.key(candidate)
                        if candidate_key < key:
                            routes, key = candidate, candidate_key
                            moves[kind] += 1
                            search.moves += 1
                            search.checkpoint(routes, self.stage)
                            improved = True
                            break
                    if improved:
                        break  # Restart both neighborhoods against the new plan.
                if not improved:
                    reason = "neighborhood_exhausted"
                    break
        except _BudgetExhausted:
            pass  # Retain the last complete, strictly improving plan.
        return routes, {
            "optimization_policy": search.request.optimization_policy.value,
            "stop_reason": reason, "passes": passes,
            "schedule_attempts": self.budget.attempts,
            "max_schedule_attempts": self.budget.limit,
            "accepted_moves": sum(moves.values()),
            "accepted_reversals": moves["reversal"],
            "accepted_tail_exchanges": moves["tail_exchange"],
            "initial_objective_value": list(initial_key), "objective_value": list(key),
        }
