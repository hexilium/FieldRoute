"""Atomic insertion chains that may leave only lower dispatch tiers unassigned."""
from __future__ import annotations

from itertools import combinations
from typing import TYPE_CHECKING

from app.domain.priorities import service_priority_rank

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class _JobLimitReached(Exception):
    pass


class PriorityRepairSearch:
    """Keep higher/equal tiers and locks while making room for a missing job.

    A chain can move a peer to another engineer and evict lower-tier work there.
    Every displaced job is first offered a direct reinsertion. Only a complete
    chain can escape: required pending jobs are never published as unassigned.
    """

    def __init__(self, search: Search, budget: SearchBudget, *, max_ejections: int = 3,
                 max_job_attempts: int = 2000) -> None:
        for value, minimum, name in (
            (budget.limit, 0, "budget"), (max_ejections, 1, "max_ejections"),
            (max_job_attempts, 1, "max_job_attempts"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
                raise ValueError(f"{name} must be an integer >= {minimum}")
        if max_ejections > 3:
            raise ValueError("max_ejections must be <= 3")
        self.search, self.budget = search, budget
        self.max_ejections, self.max_job_attempts = max_ejections, max_job_attempts

    async def repair(self, routes, pending, remaining, protected, root_rank, budget):
        search = self.search
        search.check_time()
        if not pending:
            return routes
        job, *rest = pending
        protected = protected | {job.id}
        direct = await search.insert(routes, job, budget=budget)
        if direct is not None:
            result = await self.repair(direct, rest, remaining, protected, root_rank, budget)
            if result is not None:
                return result

        # These omissions are allowed even on the last permitted attempt: the
        # replacement route was already evaluated, and no new schedule is needed.
        if (service_priority_rank(job) > root_rank
                and not search.evaluator.forced[job.id]):
            return await self.repair(routes, rest, remaining, protected, root_rank, budget)
        if budget.attempts >= budget.limit:
            raise _JobLimitReached
        if remaining == 0:
            return None

        for count in range(1, remaining + 1):
            for eid in search.ids:
                search.check_time()
                if search.evaluator.eligibility(job, search.evaluator.engineers[eid]):
                    continue
                source = routes[eid]
                removable = [jid for jid in source.jobs if jid not in protected
                             and not search.evaluator.forced[jid]]
                for displaced in combinations(removable, count):
                    reduced = tuple(jid for jid in source.jobs if jid not in displaced)
                    for position in range(len(reduced) + 1):
                        search.check_time()
                        if not budget.take():
                            raise _JobLimitReached
                        # Evaluate the complete replacement; a deletion alone can
                        # be infeasible on a directed or non-metric road network.
                        sequence = reduced[:position] + (job.id,) + reduced[position:]
                        route, _ = await search.evaluator.evaluate(eid, sequence)
                        if route is None:
                            continue
                        trial = {**routes, eid: route}
                        jobs = search.order([search.by_id[jid] for jid in displaced], "scarcity")
                        orders = [jobs] if count == 1 else [jobs, list(reversed(jobs))]
                        for ordered in orders:
                            result = await self.repair(
                                trial, [*rest, *ordered], remaining - count,
                                protected, root_rank, budget,
                            )
                            if result is not None:
                                return result
        return None

    async def improve(self, routes):
        from app.solvers.insertion import SearchBudget

        search = self.search
        initial_key = search.key(routes)
        moves = []
        roots = limited_jobs = 0
        reason = "attempt_limit"
        while self.budget.attempts < self.budget.limit:
            search.check_time()
            if not search.has_service_priority_conflict(routes):
                reason = "no_priority_conflict"
                break
            assigned = set().union(*(r.assigned_job_ids for r in routes.values()))
            removable = assigned & search._job_ids - search._locked_ids
            lowest_rank = max((service_priority_rank(search.by_id[jid]) for jid in removable),
                              default=-1)
            missing = search.order([
                job for job in search.jobs
                if job.id not in assigned and service_priority_rank(job) < lowest_rank
            ], "scarcity")
            pass_limited = False
            for index, job in enumerate(missing):
                available = self.budget.limit - self.budget.attempts
                if not available:
                    break
                quota = min(self.max_job_attempts, max(1, available // (len(missing) - index)))
                local = SearchBudget(quota)
                roots += 1
                candidate = None
                try:
                    candidate = await self.repair(
                        routes, [job], self.max_ejections, frozenset(),
                        service_priority_rank(job), local,
                    )
                except _JobLimitReached:
                    limited_jobs += 1
                    pass_limited = True
                finally:
                    self.budget.attempts += local.attempts
                if candidate is None or search.key(candidate) >= search.key(routes):
                    continue
                after = set().union(*(r.assigned_job_ids for r in candidate.values()))
                # Defensive boundary: neither required peers nor hard locks may
                # disappear, even if the broader objective would accept that plan.
                dropped = assigned - after
                if any(jid in search._locked_ids or service_priority_rank(search.by_id[jid])
                       <= service_priority_rank(job) for jid in dropped):
                    raise RuntimeError("priority repair lost a protected job")
                old_owners = {jid: eid for eid, r in routes.items() for jid in r.jobs}
                new_owners = {jid: eid for eid, r in candidate.items() for jid in r.jobs}
                moves.append({
                    "assigned_job": job.id,
                    "unassigned_jobs": sorted(dropped),
                    "reassigned_jobs": sorted(jid for jid in old_owners.keys() & new_owners.keys()
                                              if old_owners[jid] != new_owners[jid]),
                })
                routes = candidate
                search.moves += 1
                search.checkpoint(routes, "priority_repair")
                break
            else:
                reason = "job_attempt_limit" if pass_limited else "neighborhood_exhausted"
                break
        return routes, {
            "optimization_policy": search.request.optimization_policy.value,
            "stop_reason": reason, "schedule_attempts": self.budget.attempts,
            "max_schedule_attempts": self.budget.limit,
            "max_ejections": self.max_ejections, "max_job_attempts": self.max_job_attempts,
            "repair_attempts": roots, "limited_jobs": limited_jobs,
            "accepted_moves": len(moves), "moves": moves,
            "initial_objective_value": list(initial_key), "objective_value": list(search.key(routes)),
        }
