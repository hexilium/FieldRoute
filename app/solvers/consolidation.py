"""Close an engineer's route by rebuilding it together with eligible active routes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.domain.models import OptimizationPolicy
from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class ConsolidationSearch:
    def __init__(self, search: Search, budget: SearchBudget) -> None:
        if budget.limit < 0:
            raise ValueError("schedule attempt limit must be nonnegative")
        self.search = search
        self.budget = budget

    async def insert(self, routes, job, receivers):
        self.search.check_time()
        best, best_key = None, None
        for eid in receivers:
            if self.search.evaluator.eligibility(job, self.search.evaluator.engineers[eid]):
                continue
            old = routes[eid]
            for position in range(len(old.jobs) + 1):
                if not self.budget.take():
                    return best  # The best fully evaluated insertion is still usable.
                sequence = old.jobs[:position] + (job.id,) + old.jobs[position:]
                route, _ = await self.search.evaluator.evaluate(eid, sequence)
                if route is None:
                    continue
                candidate = {**routes, eid: route}
                key = self.search.key(candidate)
                if best_key is None or key < best_key:
                    best, best_key = candidate, key
        return best

    async def improve(self, routes: dict[str, Schedule]) -> tuple[dict[str, Schedule], dict]:
        search, evaluator = self.search, self.search.evaluator
        initial_key = search.key(routes)
        key = initial_key
        moves = []
        attempts = failures = passes = 0
        reason = "attempt_limit"
        modes = ["scarcity", "deadline", "priority"]
        if search.request.optimization_policy == OptimizationPolicy.sla_first:
            modes.append("sla")
        while self.budget.attempts < self.budget.limit:
            passes += 1
            improved = False
            for source in sorted(search.ids, key=lambda eid: (len(routes[eid].jobs), eid)):
                search.check_time()
                old = routes[source]
                if not old.jobs or source in evaluator.executing:
                    continue
                if any(evaluator.forced[jid] for jid in old.jobs):
                    continue
                receivers = [
                    eid for eid in search.ids if eid != source and routes[eid].visits
                    and any(not evaluator.eligibility(search.by_id[jid], evaluator.engineers[eid])
                            for jid in old.jobs)
                ]
                if any(all(evaluator.eligibility(search.by_id[jid], evaluator.engineers[eid])
                           for eid in receivers) for jid in old.jobs):
                    continue  # At least one job needs this engineer or an inactive one.
                affected = [source, *receivers]
                pending = [search.by_id[jid] for eid in affected for jid in routes[eid].jobs]
                base = dict(routes)
                for eid in affected:
                    if not self.budget.take():
                        break
                    base[eid], _ = await evaluator.evaluate(eid, ())
                    assert base[eid] is not None  # Executing visits remain in empty future routes.
                else:
                    orders = set()
                    for mode in modes:
                        ordered = search.order(pending, mode)
                        ids = tuple(job.id for job in ordered)
                        if ids in orders:
                            continue
                        orders.add(ids)
                        attempts += 1
                        trial = base
                        for job in ordered:
                            candidate = await self.insert(trial, job, receivers)
                            if candidate is None:
                                failures += 1
                                break
                            trial = candidate
                        else:
                            candidate_key = search.key(trial)
                            if candidate_key < key:
                                removed = sum(bool(r.visits) for r in routes.values()) - sum(
                                    bool(r.visits) for r in trial.values()
                                )
                                moves.append({"closed_engineer": source, "rebuilt_engineers": receivers,
                                              "order": mode, "engineers_removed": removed})
                                routes, key = trial, candidate_key
                                search.moves += 1
                                search.checkpoint(routes, "consolidation")
                                improved = True
                                break
                        if self.budget.attempts >= self.budget.limit:
                            break
                if improved or self.budget.attempts >= self.budget.limit:
                    break
            if improved:
                continue  # The accepted plan is complete; retry closing routes from this plan.
            if self.budget.attempts < self.budget.limit:
                reason = "neighborhood_exhausted"
            break
        return routes, {
            "optimization_policy": search.request.optimization_policy.value,
            "stop_reason": reason, "passes": passes,
            "schedule_attempts": self.budget.attempts, "max_schedule_attempts": self.budget.limit,
            "reconstruction_attempts": attempts, "failed_reconstructions": failures,
            "accepted_moves": len(moves), "moves": moves,
            "engineers_removed": sum(move["engineers_removed"] for move in moves),
            "initial_objective_value": list(initial_key), "objective_value": list(key),
        }
