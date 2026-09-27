"""A bounded residual candidate reconstructed from the published plan."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.domain.models import PlanRequest
from app.solvers.scheduling import Schedule, ScheduleFailure

if TYPE_CHECKING:
    from app.domain.models import Job
    from app.solvers.insertion import Search, SearchBudget


class _BudgetExhausted(Exception):
    pass


class StabilityRepairSearch:
    """Keep feasible published assignments and repair only the remaining work.

    The projected jobs retain their previous engineer and relative order. Missing
    and new jobs may be inserted around them, but an accepted insertion never
    removes or relocates a job that was already projected. Published service
    starts are soft preferences and survive whenever the current route has enough
    slack.
    """

    stage = "stability_repair"

    def __init__(self, search: Search, budget: SearchBudget) -> None:
        if (
            isinstance(budget.limit, bool)
            or not isinstance(budget.limit, int)
            or budget.limit < 0
        ):
            raise ValueError("schedule attempt limit must be a nonnegative integer")
        self.search = search
        self.budget = budget

    @staticmethod
    def enabled(request: PlanRequest) -> bool:
        """Only residual profiles that value stability need this candidate."""
        return request.previous_plan is not None and (
            request.freeze_horizon_minutes > 0
            or request.weights.plan_churn > 0
            or request.weights.schedule_shift > 0
        )

    def preferred_starts(self, job_ids: tuple[str, ...]) -> dict:
        old_stops = self.search.evaluator.old_stops
        return {
            job_id: old_stops[job_id].service_start
            for job_id in job_ids
            if job_id in old_stops
        }

    async def evaluate(
        self, engineer_id: str, job_ids: tuple[str, ...],
    ) -> tuple[Schedule | None, ScheduleFailure | None]:
        self.search.check_time()
        if not self.budget.take():
            raise _BudgetExhausted
        return await self.search.evaluator.evaluate_with_preferred_starts(
            engineer_id,
            job_ids,
            self.preferred_starts(job_ids),
        )

    async def best_insertion(
        self, routes: dict[str, Schedule], job: Job,
    ) -> tuple[dict[str, Schedule] | None, bool]:
        """Return the best complete insertion and whether its sweep hit the budget."""
        search = self.search
        best = None
        best_key = None
        for engineer_id in search.ids:
            search.check_time()
            engineer = search.evaluator.engineers[engineer_id]
            if search.evaluator.eligibility(job, engineer):
                continue
            current = routes[engineer_id]
            for position in range(len(current.jobs) + 1):
                sequence = (
                    current.jobs[:position]
                    + (job.id,)
                    + current.jobs[position:]
                )
                try:
                    changed, _ = await self.evaluate(engineer_id, sequence)
                except _BudgetExhausted:
                    return best, True
                if changed is None:
                    continue
                candidate = {**routes, engineer_id: changed}
                key = search.key(candidate)
                if best_key is None or key < best_key:
                    best, best_key = candidate, key
        return best, False

    def timing_counts(self, routes: dict[str, Schedule]) -> tuple[int, int]:
        retained = shifted = 0
        old_stops = self.search.evaluator.old_stops
        for route in routes.values():
            for visit in route.visits:
                old = old_stops.get(visit.job.id)
                if old is None or visit.executing:
                    continue
                if visit.start == old.service_start:
                    retained += 1
                else:
                    shifted += 1
        return retained, shifted

    def diagnostics(
        self,
        initial: dict[str, Schedule],
        result: dict[str, Schedule],
        *,
        reason: str,
        projection_complete: bool,
        projected: list[str],
        rejected: list[dict[str, Any]],
        inserted: list[str],
        candidate: dict[str, Schedule] | None,
        accepted: bool,
    ) -> dict[str, Any]:
        retained, shifted = self.timing_counts(candidate or result)
        assigned = (
            set().union(*(route.assigned_job_ids for route in candidate.values()))
            if candidate is not None
            else set()
        )
        return {
            "optimization_policy": self.search.request.optimization_policy.value,
            "enabled": self.enabled(self.search.request),
            "stop_reason": reason,
            "projection_complete": projection_complete,
            "schedule_attempts": self.budget.attempts,
            "max_schedule_attempts": self.budget.limit,
            "previous_jobs_considered": len(projected) + len(rejected),
            "projected_jobs": projected,
            "rejected_previous_jobs": rejected,
            "inserted_jobs": inserted,
            "uninserted_jobs": [
                job.id for job in self.search.jobs if job.id not in assigned
            ] if candidate is not None else [],
            "preserved_service_starts": retained,
            "shifted_service_starts": shifted,
            "accepted_moves": int(accepted),
            "initial_objective_value": list(self.search.key(initial)),
            "candidate_objective_value": (
                list(self.search.key(candidate)) if candidate is not None else None
            ),
            "objective_value": list(self.search.key(result)),
        }

    async def improve(
        self, routes: dict[str, Schedule],
    ) -> tuple[dict[str, Schedule], dict[str, Any]]:
        search = self.search
        if not self.enabled(search.request):
            return routes, self.diagnostics(
                routes,
                routes,
                reason="disabled",
                projection_complete=False,
                projected=[],
                rejected=[],
                inserted=[],
                candidate=None,
                accepted=False,
            )

        previous = search.request.previous_plan
        assert previous is not None
        projected: list[str] = []
        rejected: list[dict[str, Any]] = []
        inserted: list[str] = []
        candidate: dict[str, Schedule] = {}

        try:
            # Build every route before exposing a candidate. Empty future routes
            # still retain the evaluator's fixed in-progress prefix.
            for engineer_id in search.ids:
                empty, failure = await self.evaluate(engineer_id, ())
                if empty is None:
                    rejected.append({
                        "job_id": None,
                        "previous_engineer_id": engineer_id,
                        "reason": failure.code if failure else "empty_route",
                    })
                    return routes, self.diagnostics(
                        routes,
                        routes,
                        reason="projection_failed",
                        projection_complete=False,
                        projected=projected,
                        rejected=rejected,
                        inserted=inserted,
                        candidate=None,
                        accepted=False,
                    )
                candidate[engineer_id] = empty

            current_engineers = set(search.ids)
            seen: set[str] = set()
            for old_route in previous.routes:
                engineer_id = old_route.engineer_id
                for stop in old_route.stops:
                    job_id = stop.job_id
                    if stop.departure <= search.request.planning_time or job_id not in search.by_id:
                        continue
                    if job_id in seen:
                        rejected.append({
                            "job_id": job_id,
                            "previous_engineer_id": engineer_id,
                            "reason": "duplicate_previous_assignment",
                        })
                        continue
                    seen.add(job_id)
                    if engineer_id not in current_engineers:
                        rejected.append({
                            "job_id": job_id,
                            "previous_engineer_id": engineer_id,
                            "reason": "previous_engineer_missing",
                        })
                        continue
                    sequence = candidate[engineer_id].jobs + (job_id,)
                    changed, failure = await self.evaluate(engineer_id, sequence)
                    if changed is None:
                        rejected.append({
                            "job_id": job_id,
                            "previous_engineer_id": engineer_id,
                            "reason": failure.code if failure else "infeasible",
                        })
                        continue
                    candidate[engineer_id] = changed
                    projected.append(job_id)
        except _BudgetExhausted:
            return routes, self.diagnostics(
                routes,
                routes,
                reason="attempt_limit",
                projection_complete=False,
                projected=projected,
                rejected=rejected,
                inserted=inserted,
                candidate=None,
                accepted=False,
            )

        assigned = set().union(*(route.assigned_job_ids for route in candidate.values()))
        missing = search.order(
            [job for job in search.jobs if job.id not in assigned],
            "scarcity",
        )
        checkpointed = False
        if search.key(candidate) < search.key(routes):
            # Projection is now complete and therefore safe to expose to the
            # shared deadline. Keep every later improvement too: a deadline in
            # the next insertion sweep must not discard an already better plan.
            search.checkpoint(candidate, self.stage)
            checkpointed = True
        exhausted = False
        for job in missing:
            best, exhausted = await self.best_insertion(candidate, job)
            if best is not None and search.key(best) < search.key(candidate):
                candidate = best
                inserted.append(job.id)
                if search.key(candidate) < search.key(routes):
                    search.checkpoint(candidate, self.stage)
                    checkpointed = True
            if exhausted:
                break

        accepted = search.key(candidate) < search.key(routes)
        result = candidate if accepted else routes
        if accepted:
            search.moves += 1
            if not checkpointed:
                search.checkpoint(result, self.stage)
        reason = "attempt_limit" if exhausted else ("improved" if accepted else "not_improving")
        return result, self.diagnostics(
            routes,
            result,
            reason=reason,
            projection_complete=True,
            projected=projected,
            rejected=rejected,
            inserted=inserted,
            candidate=candidate,
            accepted=accepted,
        )
