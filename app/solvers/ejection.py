"""Bounded insertion chains that retain every previously assigned visit."""

from __future__ import annotations

from itertools import combinations
from typing import TYPE_CHECKING

from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class _JobBudgetExhausted(Exception):
    pass


class EjectionSearch:
    stage = "ejection"

    def __init__(
        self, search: Search, budget: SearchBudget, *, max_ejections: int = 2,
        max_job_attempts: int = 2000,
    ) -> None:
        if (
            budget.limit < 0
            or isinstance(max_ejections, bool)
            or not isinstance(max_ejections, int)
            or not 1 <= max_ejections <= 3
            or max_job_attempts < 1
        ):
            raise ValueError("invalid ejection search budget or depth")
        self.search, self.budget = search, budget
        self.max_ejections, self.max_job_attempts = max_ejections, max_job_attempts

    async def insertions(self, routes, job, pending, budget):
        """The v8 neighborhood keeps its single best direct insertion."""
        direct = await self.search.insert(routes, job, budget=budget)
        if direct is not None:
            yield direct

    async def repair(self, routes, pending, remaining, protected, budget, moves):
        """Depth-first repair; only a plan with an empty pending list can escape.

        A placed job is protected for the rest of this branch, preventing cycles.
        Several jobs can be removed together up to the configured depth when a
        smaller ejection cannot make room.
        Reduced routes need not be feasible on a directed/non-metric network:
        evaluate the complete replacement before trying to reinsert displaced work.
        """
        search = self.search
        search.check_time()
        if not pending:
            return routes, moves
        job, *rest = pending
        protected = protected | {job.id}
        async for direct in self.insertions(routes, job, rest, budget):
            result = await self.repair(direct, rest, remaining, protected, budget, moves)
            if result is not None:
                return result
        if budget.attempts >= budget.limit:
            raise _JobBudgetExhausted
        if remaining == 0:
            return None
        for count in range(1, remaining + 1):
            for eid in search.ids:
                search.check_time()
                if search.evaluator.eligibility(job, search.evaluator.engineers[eid]):
                    continue
                source = routes[eid]
                removable = [jid for jid in source.jobs if jid not in protected]
                for displaced in combinations(removable, count):
                    reduced = tuple(jid for jid in source.jobs if jid not in displaced)
                    for position in range(len(reduced) + 1):
                        if not budget.take():
                            raise _JobBudgetExhausted
                        sequence = reduced[:position] + (job.id,) + reduced[position:]
                        route, _ = await search.evaluator.evaluate(eid, sequence)
                        if route is None:
                            continue
                        trial = {**routes, eid: route}
                        jobs = search.order([search.by_id[jid] for jid in displaced], "scarcity")
                        # For multiple displaced jobs, try both deterministic
                        # greedy reinsertion orders rather than every permutation.
                        orders = [jobs] if count == 1 else [jobs, list(reversed(jobs))]
                        for ordered in orders:
                            result = await self.repair(
                                trial, [*rest, *ordered], remaining - count, protected, budget,
                                [*moves, {"engineer_id": eid, "inserted_job": job.id,
                                          "displaced_jobs": list(displaced)}],
                            )
                            if result is not None:
                                return result
        return None

    async def improve(self, routes: dict[str, Schedule]) -> tuple[dict[str, Schedule], dict]:
        from app.solvers.insertion import SearchBudget

        search = self.search
        initial_key = search.key(routes)
        moves = []
        passes = roots = limited_jobs = 0
        reason = "attempt_limit"
        while self.budget.attempts < self.budget.limit:
            present = {jid for route in routes.values() for jid in route.jobs}
            missing = search.order([j for j in search.jobs if j.id not in present], "scarcity")
            if not missing:
                reason = "all_assigned"
                break
            passes += 1
            improved = False
            pass_limited = False
            for index, job in enumerate(missing):
                available = self.budget.limit - self.budget.attempts
                if not available:
                    break
                # Give every remaining missing job a share; the first difficult
                # request cannot consume the entire stage's schedule budget.
                quota = min(self.max_job_attempts, max(1, available // (len(missing) - index)))
                local = SearchBudget(quota)
                roots += 1
                candidate = None
                try:
                    candidate = await self.repair(routes, [job], self.max_ejections,
                                                  frozenset(), local, [])
                except _JobBudgetExhausted:
                    limited_jobs += 1
                    pass_limited = True
                finally:
                    self.budget.attempts += local.attempts
                if candidate is not None:
                    trial, chain = candidate
                    if search.key(trial) < search.key(routes):
                        routes = trial
                        moves.append({"assigned_job": job.id, "chain": chain,
                                      "displaced_jobs": sum(len(m["displaced_jobs"]) for m in chain)})
                        search.moves += 1
                        search.checkpoint(routes, self.stage)
                        improved = True
                        break
            if not improved:
                if self.budget.attempts < self.budget.limit:
                    reason = "job_attempt_limit" if pass_limited else "neighborhood_exhausted"
                break
        return routes, {
            "optimization_policy": search.request.optimization_policy.value,
            "stop_reason": reason, "passes": passes,
            "schedule_attempts": self.budget.attempts, "max_schedule_attempts": self.budget.limit,
            "max_ejections": self.max_ejections, "max_job_attempts": self.max_job_attempts,
            "repair_attempts": roots, "limited_jobs": limited_jobs,
            "accepted_moves": len(moves), "added_jobs": len(moves), "moves": moves,
            "initial_objective_value": list(initial_key), "objective_value": list(search.key(routes)),
        }
