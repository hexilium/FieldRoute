"""Deterministic insertion search with route elimination and relocation."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from time import perf_counter
from typing import TYPE_CHECKING

from app.domain.models import (
    DistrictMode,
    EngineerRoute,
    Job,
    JobDecision,
    OptimizationPolicy,
    PlannedStop,
    PlanRequest,
    PlanResult,
    ServicePriorityPolicy,
    UnassignedJob,
    UrgencyPolicy,
    UrgentStartPolicy,
)
from app.domain.priorities import (
    is_emergency,
    missing_priority_counts,
    priority_rank,
    service_priority_rank,
)
from app.routing.base import RoutingProvider
from app.services.explainer import assignment_explanation, at, minutes_text
from app.services.metrics import build_metrics
from app.services.plan_diff import attach_previous_diff
from app.services.progress import current_progress
from app.services.validation import validate_plan
from app.solvers.base import Solver
from app.solvers.compound import CompoundSearch
from app.solvers.consolidation import ConsolidationSearch
from app.solvers.cross_exchange import CrossExchangeSearch
from app.solvers.cyclic_exchange import CyclicExchangeSearch
from app.solvers.cyclic_reinsertion import CyclicReinsertionSearch
from app.solvers.deadline import SearchDeadline, SearchTimeLimitReached
from app.solvers.deep_repair import DeepRepairSearch
from app.solvers.ejection import EjectionSearch
from app.solvers.or_opt import OrOptSearch
from app.solvers.priority_repair import PriorityRepairSearch
from app.solvers.repair import RepairSearch
from app.solvers.ruin_recreate import RuinRecreateSearch
from app.solvers.scheduling import Schedule, ScheduleEvaluator
from app.solvers.segments import RouteSegmentSearch
from app.solvers.stability_search import StabilityRepairSearch

if TYPE_CHECKING:
    from app.solvers.adaptive import AttemptController

REASONS = {
    "district_mismatch": "заявка относится к другому району; выезды запрещены",
    "district_no_engineer": "в районе заявки нет инженеров",
    "engineer_unavailable": "инженер недоступен",
    "skills": "не хватает требуемых навыков",
    "equipment": "нет необходимого оборудования",
    "transport": "нет требуемого транспорта",
    "locked_to_other_engineer": "заявка закреплена за другим инженером",
    "max_jobs": "достигнут заданный лимит заявок",
    "unreachable": "нет доступного пути до точки",
    "time_window": "визит не помещается в окно по выбранному правилу начала/завершения работ",
    "shift_end": "работа заканчивается после смены",
    "no_engineer": "нет инженеров",
    "search_limit": "ограниченный поиск не включил заявку при наличии допустимой вставки",
    "search_time_limit": "поиск остановлен по лимиту времени",
    "search_attempt_limit": "исчерпан бюджет проверок маршрутов",
    "search_portfolio_limit": "завершён ограниченный адаптивный поиск",
    "fifo_no_retry": "FIFO не возвращается к ранее пропущенной заявке",
    "urgent_start_tradeoff": "допустимые вставки ухудшают выбранный приоритет раннего начала срочных работ",
}


@dataclass
class SearchBudget:
    """Count schedule attempts, including cache hits, for repeatable stopping."""

    limit: int
    attempts: int = 0

    def take(self) -> bool:
        if self.attempts >= self.limit:
            return False
        self.attempts += 1
        return True


class InsertionObjective:
    """Score one replacement without rebuilding coverage or every other route.

    Every feasible insertion adds the same job to the same plan. Coverage is
    invariant across positions and engineers. Float totals still use sum over
    the original route order: subtract/add deltas would change numerical ties.
    """

    def __init__(self, search: Search, routes: dict[str, Schedule], job: Job) -> None:
        self.search = search
        assigned = set().union(*(r.assigned_job_ids for r in routes.values()), {job.id})
        missing = search._job_ids - assigned
        locked = len(missing & search._locked_ids)
        self.urgent_missing = len(missing & search._urgent_ids)
        self.coverage = search.coverage_parts(missing, locked)
        self.routes = list(routes.values())
        self.indices = {eid: index for index, eid in enumerate(routes)}
        self.distances = [r.distance for r in self.routes]
        self.costs = [r.soft_cost for r in self.routes]
        self.late = [r.late_minutes for r in self.routes]
        self.urgent_starts = [r.urgent_start_minutes for r in self.routes]
        self.used = sum(bool(r.visits) for r in self.routes)
        self.sla_missed = len(missing & search._sla_ids) + sum(
            r.sla_late_jobs for r in self.routes
        )

    def key(self, eid: str, route: Schedule) -> tuple:
        index = self.indices[eid]
        old = self.routes[index]
        self.distances[index] = route.distance
        self.costs[index] = route.soft_cost
        self.late[index] = route.late_minutes
        self.urgent_starts[index] = route.urgent_start_minutes
        km, soft = sum(self.distances), sum(self.costs)
        used = self.used - bool(old.visits) + bool(route.visits)
        search = self.search
        urgency = () if search.legacy_objective else (
            self.urgent_missing, sum(self.urgent_starts),
        )
        policy = search.request.optimization_policy
        if policy == OptimizationPolicy.distance_first:
            key = search.objective_parts(self.coverage, (km, used), urgency, (soft,))
        else:
            cost = (soft + search.request.weights.distance_km * km, km) if search.replanning else (km, soft)
            if policy == OptimizationPolicy.sla_first:
                missed = self.sla_missed - old.sla_late_jobs + route.sla_late_jobs
                key = search.objective_parts(
                    self.coverage, (missed, sum(self.late), used), urgency, cost,
                )
            else:
                key = search.objective_parts(self.coverage, (used,), urgency, cost)
        self.distances[index] = old.distance
        self.costs[index] = old.soft_cost
        self.late[index] = old.late_minutes
        self.urgent_starts[index] = old.urgent_start_minutes
        return key


class Search:
    """One request owns all mutable search state; solver instances are reusable."""

    def __init__(
        self, request: PlanRequest, routing: RoutingProvider,
        evaluator: ScheduleEvaluator | None = None,
        *, deadline: SearchDeadline | None = None, legacy_objective: bool = False,
    ) -> None:
        self.request = request
        # Only historical replay uses the v4-v10 objective. This is not an API
        # setting: current paths use the v11 objective, retained unchanged in v18.
        self.legacy_objective = legacy_objective
        self.deadline = deadline
        self.attempt_control: AttemptController | None = None
        self.progress = current_progress.get()
        self.evaluator = evaluator or ScheduleEvaluator(request, routing)
        self.ids = list(self.evaluator.engineers)
        self.jobs = self.evaluator.movable
        self.by_id = {j.id: j for j in self.jobs}
        self.replanning = request.previous_plan is not None
        self.moves = 0
        self._job_ids = frozenset(j.id for j in self.jobs)
        self._locked_ids = frozenset(j.id for j in self.jobs if self.evaluator.forced[j.id])
        self._urgent_ids = frozenset(
            j.id for j in self.jobs
            if (j.priority >= 80 if legacy_objective else is_emergency(j, request))
        )
        self._sla_ids = frozenset(j.id for j in self.jobs if j.sla_deadline is not None)

    def check_time(self) -> None:
        if self.deadline is not None:
            self.deadline.check()

    def checkpoint(self, routes: dict[str, Schedule], detail: str = "checkpoint") -> None:
        if self.attempt_control is not None:
            self.attempt_control.consider(routes, detail)
        if self.progress is not None:
            self.progress.committed(routes)
        if self.deadline is not None:
            self.deadline.consider(routes, detail)

    @property
    def may_displace_for_early_start(self) -> bool:
        return (
            not self.legacy_objective
            and self.request.urgent_start_policy == UrgentStartPolicy.before_coverage
        )

    def coverage_parts(self, missing: set[str] | frozenset[str], locked_missing: int) -> tuple:
        """Coverage priority is a lexicographic business rule, not a weighted score."""
        if not self.legacy_objective and self.request.service_priority_policy == ServicePriorityPolicy.organizer:
            emergency, connection, regular, other = missing_priority_counts(missing, self.by_id)
            return locked_missing, emergency, connection, regular, other
        urgent_missing = len(missing & self._urgent_ids) if self.replanning else 0
        return (
            (locked_missing, len(missing), urgent_missing)
            if self.request.urgency_policy == UrgencyPolicy.coverage_first
            else (locked_missing, urgent_missing, len(missing))
        )

    def has_service_priority_conflict(self, routes: dict[str, Schedule]) -> bool:
        """Whether a lower organizer tier occupies capacity while a higher tier is missing."""
        if self.legacy_objective or self.request.service_priority_policy != ServicePriorityPolicy.organizer:
            return False
        assigned = set().union(*(route.assigned_job_ids for route in routes.values()))
        missing = self._job_ids - assigned
        if not missing:
            return False
        missing_ranks = [service_priority_rank(self.by_id[job_id]) for job_id in missing]
        assigned_ranks = [service_priority_rank(self.by_id[job_id]) for job_id in assigned if job_id in self.by_id]
        if not assigned_ranks:
            return False
        best_missing = min(missing_ranks)
        return any(rank > best_missing for rank in assigned_ranks)

    def objective_parts(self, coverage, primary, urgency, cost) -> tuple:
        """Use the same criterion order for full plans, insertions and labels."""
        if not self.legacy_objective and self.request.service_priority_policy == ServicePriorityPolicy.organizer:
            # The organizer hierarchy is a hard lexicographic allocation rule.
            # Early-start preferences may outrank the economic objective, but never
            # sacrificing an Emergency for a Connection or a Connection for lower work.
            if self.request.urgent_start_policy in {
                UrgentStartPolicy.before_primary, UrgentStartPolicy.before_coverage,
            }:
                return *coverage, *urgency, *primary, *cost
            return *coverage, *primary, *urgency, *cost
        if self.may_displace_for_early_start:
            return coverage[0], *urgency, *coverage[1:], *primary, *cost
        if not self.legacy_objective and (
            self.request.urgent_start_policy == UrgentStartPolicy.before_primary
        ):
            return *coverage, *urgency, *primary, *cost
        return *coverage, *primary, *urgency, *cost

    def key(self, routes: dict[str, Schedule]) -> tuple:
        assigned = set().union(*(r.assigned_job_ids for r in routes.values()))
        missing = self._job_ids - assigned
        locked_missing = len(missing & self._locked_ids)
        # Preserve the original sums and route order, including float tie-breaking.
        used = sum(bool(r.visits) for r in routes.values())
        km = sum(r.distance for r in routes.values())
        soft = sum(r.soft_cost for r in routes.values())
        if self.replanning:
            # Dispatch stability and SLA can trade against distance in residual plans.
            cost = (soft + self.request.weights.distance_km * km, km)
        else:
            cost = (km, soft)
        coverage = self.coverage_parts(missing, locked_missing)
        urgency = () if self.legacy_objective else (
            len(missing & self._urgent_ids),
            sum(r.urgent_start_minutes for r in routes.values()),
        )
        if self.request.optimization_policy == OptimizationPolicy.distance_first:
            return self.objective_parts(coverage, (km, used), urgency, (soft,))
        if self.request.optimization_policy == OptimizationPolicy.sla_first:
            sla_missed = len(missing & self._sla_ids) + sum(
                r.sla_late_jobs for r in routes.values()
            )
            late = sum(r.late_minutes for r in routes.values())
            return self.objective_parts(coverage, (sla_missed, late, used), urgency, cost)
        return self.objective_parts(coverage, (used,), urgency, cost)

    def objective_order(self) -> list[str]:
        if not self.legacy_objective and self.request.service_priority_policy == ServicePriorityPolicy.organizer:
            order = [
                "locked_unassigned", "emergency_unassigned", "connection_unassigned",
                "repair_or_additional_unassigned", "other_unassigned",
            ]
        else:
            order = ["locked_unassigned", "urgent_unassigned_on_replan", "unassigned"]
            if self.request.urgency_policy == UrgencyPolicy.coverage_first:
                order[1], order[2] = order[2], order[1]
        urgency = [] if self.legacy_objective else ["urgent_unassigned", "urgent_start_minutes"]
        if self.request.optimization_policy == OptimizationPolicy.distance_first:
            return list(self.objective_parts(
                order, ["distance_km", "used_engineers"], urgency, ["soft_cost"],
            ))
        primary = (
            ["sla_missed_jobs", "late_minutes", "used_engineers"]
            if self.request.optimization_policy == OptimizationPolicy.sla_first
            else ["used_engineers"]
        )
        return list(self.objective_parts(order, primary, urgency, [
            "weighted_residual_cost" if self.replanning else "distance_km",
            "distance_km" if self.replanning else "soft_cost",
        ]))

    async def empty(self) -> dict[str, Schedule]:
        result = {}
        for eid in self.ids:
            route, _ = await self.evaluator.evaluate(eid, ())
            assert route is not None
            result[eid] = route
        return result

    def order(self, jobs: list[Job], mode: str) -> list[Job]:
        def rank(job: Job) -> tuple:
            self.check_time()
            mandatory = 0 if self.evaluator.forced[job.id] else 1
            dispatch = (
                priority_rank(job, self.request)
                if not self.legacy_objective and self.request.service_priority_policy == ServicePriorityPolicy.organizer
                else ()
            )
            urgent = 0 if (
                (self.replanning or self.may_displace_for_early_start) and job.id in self._urgent_ids
            ) else 1
            eligible = sum(
                not self.evaluator.eligibility(job, e) for e in self.evaluator.engineers.values()
            )
            deadline = min((w.end.timestamp() for w in job.time_windows), default=float("inf"))
            if mode == "sla":
                deadline = (
                    min(deadline, job.sla_deadline.timestamp()) if job.sla_deadline else deadline
                )
                tail = (deadline, eligible, -job.priority)
            elif mode == "scarcity":
                tail = (eligible, deadline, -job.priority)
            elif mode == "priority":
                tail = (-job.priority, deadline, eligible)
            else:
                tail = (deadline, eligible, -job.priority)
            return mandatory, *dispatch, urgent, *tail, job.id

        return sorted(jobs, key=rank)

    async def insert(
        self, routes: dict[str, Schedule], job: Job, *, exclude: str | None = None,
        budget: SearchBudget | None = None,
    ) -> dict[str, Schedule] | None:
        self.check_time()
        best = None
        best_key = None
        objective = InsertionObjective(self, routes, job)
        for eid in self.ids:
            if eid == exclude or self.evaluator.eligibility(job, self.evaluator.engineers[eid]):
                continue
            old = routes[eid]
            for pos in range(len(old.jobs) + 1):
                if budget is not None and not budget.take():
                    return best
                candidate = await self.evaluator.evaluate_insertion(eid, old, job.id, pos)
                if candidate is None:
                    continue
                key = objective.key(eid, candidate)
                if best_key is None or key < best_key:
                    best, best_key = {**routes, eid: candidate}, key
        return best

    async def baseline(self) -> dict[str, Schedule]:
        routes = await self.empty()
        for index, job in enumerate(self.jobs, 1):  # Input order represents arrival order.
            for eid in self.ids:  # First feasible engineer; append only.
                candidate, _ = await self.evaluator.evaluate(eid, routes[eid].jobs + (job.id,))
                if candidate is not None:
                    routes[eid] = candidate
                    self.checkpoint(routes, "baseline")
                    break
            if self.progress is not None:
                self.progress.update(processed_jobs=index, pass_name="fifo")
        if self.deadline is not None:
            self.deadline.baseline = dict(routes)
        return routes

    async def construct(self, mode: str) -> dict[str, Schedule]:
        routes = await self.empty()
        for index, job in enumerate(self.order(self.jobs, mode), 1):
            updated = await self.insert(routes, job)
            if updated is not None and (
                not self.may_displace_for_early_start or self.key(updated) < self.key(routes)
            ):
                routes = updated
                self.checkpoint(routes, f"construct:{mode}")
            if self.progress is not None:
                self.progress.update(processed_jobs=index, pass_name=mode)
        return routes

    async def improve(self, routes: dict[str, Schedule]) -> dict[str, Schedule]:
        # Bounded passes guarantee termination; every committed change improves the key.
        for _ in range(3):
            start_key = self.key(routes)
            present = {jid for r in routes.values() for jid in r.jobs}
            missing = self.order([j for j in self.jobs if j.id not in present], "scarcity")
            for job in missing:
                candidate = await self.insert(routes, job)
                if candidate is not None and self.key(candidate) < self.key(routes):
                    routes = candidate
                    self.moves += 1
                    self.checkpoint(routes)

            # Empty a whole route atomically. Never lose jobs to save an engineer.
            for eid in sorted(self.ids, key=lambda e: (len(routes[e].jobs), e)):
                source = routes[eid]
                if not source.jobs or eid in self.evaluator.executing:
                    continue
                if any(self.evaluator.forced[jid] for jid in source.jobs):
                    continue
                empty, _ = await self.evaluator.evaluate(eid, ())
                assert empty is not None
                trial = {**routes, eid: empty}
                for job in self.order([self.by_id[j] for j in source.jobs], "scarcity"):
                    candidate = await self.insert(trial, job, exclude=eid)
                    if candidate is None:
                        break
                    trial = candidate
                else:
                    if self.key(trial) < self.key(routes):
                        routes = trial
                        self.moves += 1
                        self.checkpoint(routes)

            # Relocate one visit (also within its route), fully rescheduling both routes.
            # At most one successful move per source route in a pass keeps work bounded.
            for eid in self.ids:
                source = routes[eid]
                for jid in source.jobs:
                    reduced, _ = await self.evaluator.evaluate(
                        eid, tuple(j for j in source.jobs if j != jid)
                    )
                    if reduced is None:
                        continue  # Non-metric travel may make deletion infeasible.
                    trial = {**routes, eid: reduced}
                    candidate = await self.insert(trial, self.by_id[jid])
                    if candidate is not None and self.key(candidate) < self.key(routes):
                        routes = candidate
                        self.moves += 1
                        self.checkpoint(routes)
                        break
            if self.key(routes) >= start_key:
                break
        return routes

    async def polish(
        self, routes: dict[str, Schedule], *, max_attempts: int = 25_000,
    ) -> tuple[dict[str, Schedule], dict]:
        """Repeat single-job insertions/relocations until a quiet sweep or budget.

        Each job sees the latest routes, so several visits of one engineer can
        move in a sweep. Only full, strictly better plans are committed, including
        when a budget interrupts the enumeration of insertion positions.
        """
        if max_attempts < 0:
            raise ValueError("max_attempts must be nonnegative")
        budget = SearchBudget(max_attempts)
        initial_key = self.key(routes)
        moves = passes = 0
        reason = "attempt_limit"
        ordered = self.order(self.jobs, "scarcity")
        while budget.attempts < budget.limit:
            passes += 1
            start_moves = moves
            for job in ordered:
                owner = next((eid for eid in self.ids if job.id in routes[eid].jobs), None)
                trial = routes
                if owner is not None:
                    if not budget.take():
                        break
                    reduced, _ = await self.evaluator.evaluate(
                        owner, tuple(jid for jid in routes[owner].jobs if jid != job.id)
                    )
                    if reduced is None:
                        continue  # Deletion itself may fail with non-metric travel.
                    trial = {**routes, owner: reduced}
                candidate = await self.insert(trial, job, budget=budget)
                if candidate is not None and self.key(candidate) < self.key(routes):
                    routes = candidate
                    moves += 1
                    self.moves += 1
                    self.checkpoint(routes)
                if budget.attempts >= budget.limit:
                    break
            else:
                if moves == start_moves:
                    reason = "neighborhood_exhausted"
                    break
        return routes, {
            "optimization_policy": self.request.optimization_policy.value,
            "stop_reason": reason,
            "passes": passes,
            "schedule_attempts": budget.attempts,
            "max_schedule_attempts": budget.limit,
            "accepted_moves": moves,
            "initial_objective_value": list(initial_key),
            "objective_value": list(self.key(routes)),
        }

    async def optimize(
        self, seed: dict[str, Schedule] | None = None
    ) -> tuple[dict[str, Schedule], tuple]:
        baseline = await self.baseline()
        baseline_key = self.key(baseline)
        best = seed if seed is not None and self.key(seed) < baseline_key else baseline
        modes = ["scarcity", "deadline", "priority"]
        if self.request.optimization_policy == OptimizationPolicy.sla_first:
            modes.append("sla")
        for mode in modes:
            candidate = await self.construct(mode)
            if self.key(candidate) < self.key(best):
                best = candidate
        return await self.improve(best), baseline_key

    async def candidate(self, stage: str, seed=None):
        """One independent stage; the coordinator fixes seeds and merge order."""
        if stage == "priority_repair":
            return await PriorityRepairSearch(self, SearchBudget(10_000)).improve(seed)
        if stage == "construction":
            baseline = await self.baseline()
            best = baseline
            modes = ["scarcity", "deadline", "priority"]
            if self.request.optimization_policy == OptimizationPolicy.sla_first:
                modes.append("sla")
            for mode in modes:
                candidate = await self.construct(mode)
                if self.key(candidate) < self.key(best):
                    best = candidate
            return best, self.key(baseline)
        if stage == "initial_finish":
            best, baseline_key, warm_start = seed
            # In optimize(), the staff seed precedes constructed candidates and
            # wins their ties, but only when it strictly improves on FIFO.
            if (warm_start is not None and self.key(warm_start) < baseline_key
                    and self.key(warm_start) <= self.key(best)):
                best = warm_start
            return await self.improve(best), None
        if stage == "initial":
            routes, _ = await self.optimize(seed)
            return routes, None
        if stage == "refined":
            return await self.improve(seed), None
        if stage == "polished":
            return await self.polish(seed)
        if stage == "urgent":
            initial_key = self.key(seed)
            before_moves = self.moves
            attempts = self.evaluator.attempts
            routes, finish = seed, None
            if self._urgent_ids:
                # Retain the best v10 seed. Fresh constructions also find early
                # emergency plans that intentionally omit delaying normal work.
                routes, _ = await self.optimize(seed)
                routes, finish = await self.polish(routes, max_attempts=10_000)
            return routes, {
                "optimization_policy": self.request.optimization_policy.value,
                "urgent_start_policy": self.request.urgent_start_policy.value,
                "objective_order": self.objective_order(),
                "initial_objective_value": list(initial_key),
                "objective_value": list(self.key(routes)),
                "accepted_moves": self.moves - before_moves,
                "schedule_attempts": self.evaluator.attempts - attempts,
                "stop_reason": "completed" if self._urgent_ids else "no_future_urgent_jobs",
                "final_polishing": finish,
            }
        seed_origin = None
        seed_policy = None
        if stage == "stability_repair":
            seed_origin, seed = seed
            routes, diagnostics = await StabilityRepairSearch(
                self, SearchBudget(10_000),
            ).improve(seed)
            return routes, {
                **diagnostics,
                "seed_origin": seed_origin,
                "final_objective_value": list(self.key(routes)),
            }
        if stage in {
            "or_opt", "cross_exchange", "deep_repair", "cyclic_exchange",
            "cyclic_reinsertion", "ruin_recreate",
        }:
            seed_origin, seed = seed
            seed_policy = seed_origin.split(":", 1)[0]
        implementation = {"compound": CompoundSearch, "consolidated": ConsolidationSearch,
                          "ejection": EjectionSearch, "repair": RepairSearch,
                          "segments": RouteSegmentSearch, "or_opt": OrOptSearch,
                          "cross_exchange": CrossExchangeSearch,
                          "deep_repair": DeepRepairSearch,
                          "cyclic_exchange": CyclicExchangeSearch,
                          "cyclic_reinsertion": CyclicReinsertionSearch,
                          "ruin_recreate": RuinRecreateSearch}[stage]
        routes, diagnostics = await implementation(self, SearchBudget(10_000)).improve(seed)
        finish = None
        if diagnostics["accepted_moves"]:
            InsertionSolver._enter(
                self.deadline, self.request.optimization_policy.value + ":" + stage + ":finish",
            )
            routes, finish = await self.polish(routes, max_attempts=5_000)
        cross_policy_refinement = None
        if (
            stage in {
                "or_opt", "cross_exchange", "deep_repair", "cyclic_exchange",
                "cyclic_reinsertion", "ruin_recreate",
            }
            and seed_policy != self.request.optimization_policy.value
            and not (
                not self._sla_ids
                and {seed_policy, self.request.optimization_policy.value}
                <= {OptimizationPolicy.staff_first.value, OptimizationPolicy.sla_first.value}
            )
        ):
            # A higher-priority coverage result can make another policy's own
            # candidate lose at the final barrier. Revisit the inherited plan
            # with this policy's normal neighborhoods while coverage remains first.
            InsertionSolver._enter(
                self.deadline,
                self.request.optimization_policy.value + ":" + stage + ":cross_policy",
            )
            initial_key = self.key(routes)
            before_moves = self.moves
            before_attempts = self.evaluator.attempts
            routes = await self.improve(routes)
            cross_policy_refinement = {
                "seed_origin": seed_origin,
                "initial_objective_value": list(initial_key),
                "objective_value": list(self.key(routes)),
                "accepted_moves": self.moves - before_moves,
                "schedule_attempts": self.evaluator.attempts - before_attempts,
            }
        return routes, {
            **diagnostics, "final_polishing": finish,
            "cross_policy_refinement": cross_policy_refinement,
            "final_objective_value": list(self.key(routes)),
        }

    async def result(
        self, routes: dict[str, Schedule], solver: str, started: float, baseline_key: tuple | None,
        *, search_stopped: bool = False, manual_assignment: str | None = None,
        search_stop_reason: str = "search_time_limit",
        unassigned_scope: tuple[str, str] | None = None,
    ) -> PlanResult:
        if self.progress is not None:
            self.progress.update(force=True, phase="finalization")
        output = []
        decisions = []
        assigned = set()
        for eid in self.ids:
            engineer = self.evaluator.engineers[eid]
            schedule = routes[eid]
            stops = []
            for visit in schedule.visits:
                job = visit.job
                frozen = bool(self.evaluator.forced[job.id]) or visit.executing
                explanation = assignment_explanation(
                    job, engineer, visit.minutes, arrival=visit.arrival,
                    start=visit.start, end=visit.end,
                )
                if visit.executing:
                    explanation = [
                        f"работа уже выполняется: {at(visit.start)} — {at(visit.end)}",
                        "инженер занят ещё "
                        + minutes_text((visit.end - self.request.planning_time).total_seconds() / 60)
                        + " мин; интервал сохраняется",
                    ]
                elif frozen:
                    explanation.insert(
                        0, "сохранён закреплённый исполнитель; время может измениться"
                    )
                if job.sla_deadline and visit.executing:
                    explanation.append(
                        "SLA по времени начала работ соблюдается"
                        if visit.start <= job.sla_deadline
                        else "начало работ позже срока SLA"
                    )
                explanation.append(
                    "назначение первому допустимому инженеру в порядке входных данных"
                    if solver == "fifo-baseline-v1"
                    else "проверены позиции вставки и расписание всего маршрута; "
                    "план выбран по покрытию, числу исполнителей и стоимости маршрутов"
                )
                if (
                    solver != "fifo-baseline-v1"
                    and self.request.optimization_policy == OptimizationPolicy.distance_first
                ):
                    explanation[-1] = (
                        "при одинаковом покрытии и приоритете срочных выбран план "
                        "с меньшим пробегом; число инженеров используется следующим критерием"
                    )
                if (
                    solver != "fifo-baseline-v1"
                    and self.request.optimization_policy == OptimizationPolicy.sla_first
                ):
                    explanation[-1] = (
                        "при одинаковом покрытии выбран план с меньшим числом нарушений SLA; "
                        "для соблюдения сроков могут привлекаться дополнительные инженеры"
                    )
                if solver != "fifo-baseline-v1" and not self.legacy_objective:
                    if self.may_displace_for_early_start:
                        explanation[-1] = (
                            "после обязательных закреплений выбран приоритет назначения и "
                            "раннего начала срочных; обычные заявки могут остаться неназначенными"
                        )
                    elif self.request.urgent_start_policy == UrgentStartPolicy.before_primary:
                        explanation[-1] = (
                            "при одинаковом покрытии выбран приоритет назначения и раннего "
                            "начала срочных; могут привлекаться дополнительные инженеры"
                        )
                if (
                    solver != "fifo-baseline-v1" and not self.legacy_objective
                    and job.id in self._urgent_ids and not visit.executing
                    and manual_assignment is None
                ):
                    explanation.append(
                        "срочная работа: начало через "
                        + minutes_text(max(
                            0.0, (visit.start - self.request.planning_time).total_seconds() / 60,
                        ))
                        + " мин от момента расчёта; при выборе плана учтено "
                        "суммарное время до начала срочных работ"
                    )
                if manual_assignment is not None:
                    explanation[-1] = (
                        "исполнитель выбран диспетчером и закреплён для последующих пересчётов"
                        if job.id == manual_assignment else
                        "назначение сохранено при ручном изменении другой заявки; "
                        "времена затронутого маршрута проверены заново"
                    )
                stops.append(
                    PlannedStop(
                        job_id=job.id,
                        title=job.title,
                        location=job.location,
                        arrival=visit.arrival,
                        service_start=visit.start,
                        departure=visit.end,
                        travel_minutes_from_previous=visit.minutes,
                        distance_km_from_previous=visit.km,
                        explanation=explanation,
                        frozen=frozen,
                    )
                )
                assigned.add(job.id)
                decisions.append(JobDecision(job_id=job.id, chosen_engineer_id=eid, frozen=frozen))
            current = self.evaluator.executing.get(eid)
            origin = (
                current.job.location
                if current
                else (engineer.current_location or engineer.start_location)
            )
            future = [v.job.location for v in schedule.visits if not v.executing]
            geometry = await self.evaluator.routing.route_geometry_for_engineer(
                [origin, *future], engineer,
            )
            start = max(self.request.planning_time, engineer.shift.start)
            if engineer.available_from and current is None:
                start = max(start, engineer.available_from)
            work = max(0, (stops[-1].departure - start).total_seconds() / 60) if stops else 0
            output.append(
                EngineerRoute(
                    engineer_id=eid,
                    engineer_name=engineer.name,
                    stops=stops,
                    geometry=geometry,
                    total_distance_km=schedule.distance,
                    total_travel_minutes=schedule.travel,
                    work_minutes=work,
                )
            )

        unassigned = []
        selected_key = self.key(routes)
        for job in self.jobs:
            if job.id in assigned:
                continue
            if unassigned_scope is not None:
                code, explanation = unassigned_scope
                unassigned.append(UnassignedJob(
                    job_id=job.id, reason_codes=[code], explanation=explanation,
                ))
                decisions.append(JobDecision(job_id=job.id))
                continue
            if manual_assignment is not None:
                unassigned.append(UnassignedJob(
                    job_id=job.id, reason_codes=["manual_assignment_scope"],
                    explanation="Эта заявка оставалась неназначенной в исходном плане. "
                    "Ручная проверка меняет только выбранную заявку; для остальных "
                    "новые назначения не искались.",
                ))
                decisions.append(JobDecision(job_id=job.id))
                continue
            if search_stopped:
                unassigned.append(UnassignedJob(
                    job_id=job.id, reason_codes=[search_stop_reason],
                    explanation=REASONS[search_stop_reason].capitalize() + ". Допустимость назначения "
                    "этой заявки в итоговых маршрутах не проверена.",
                ))
                decisions.append(JobDecision(job_id=job.id))
                continue
            failures = set()
            feasible_insertion = False
            improving_insertion = False
            for eid in self.ids:
                reasons = self.evaluator.eligibility(job, self.evaluator.engineers[eid])
                if reasons:
                    failures.update(reasons)
                    continue
                old = routes[eid].jobs
                positions = [len(old)] if solver == "fifo-baseline-v1" else range(len(old) + 1)
                for pos in positions:
                    candidate, reason = await self.evaluator.evaluate(
                        eid, old[:pos] + (job.id,) + old[pos:]
                    )
                    if reason:
                        failures.add(reason)
                    else:
                        feasible_insertion = True
                        if self.may_displace_for_early_start and candidate is not None:
                            improving_insertion |= self.key({**routes, eid: candidate}) < selected_key
            codes = sorted(failures) or ["no_engineer"]
            if feasible_insertion:
                codes = ["fifo_no_retry" if solver == "fifo-baseline-v1" else "search_limit"]
                if (
                    solver != "fifo-baseline-v1" and self.may_displace_for_early_start
                    and not improving_insertion
                ):
                    codes = ["urgent_start_tradeoff"]
            unassigned.append(
                UnassignedJob(
                    job_id=job.id,
                    reason_codes=codes,
                    explanation=(
                        "В конечном плане есть допустимое место для заявки. "
                        + REASONS[codes[0]] + "."
                    ) if feasible_insertion else "Не найдена допустимая "
                    + (
                        "позиция в конце маршрута: "
                        if solver == "fifo-baseline-v1"
                        else "вставка: "
                    )
                    + "; ".join(REASONS[c] for c in codes)
                    + ". Это результат поиска в текущих маршрутах, а не доказательство невозможности.",
                )
            )
            decisions.append(JobDecision(job_id=job.id))
        changed = sum(
            1
            for route in output
            for stop in route.stops
            if stop.job_id in self.evaluator.previous
            and self.evaluator.previous[stop.job_id] != route.engineer_id
        )
        metrics = build_metrics(
            output,
            len(self.evaluator.jobs),
            len(unassigned),
            changed,
            jobs=list(self.evaluator.jobs.values()),
            planning_time=self.request.planning_time,
            previous_plan=self.request.previous_plan,
            frozen_jobs=sum(s.frozen for r in output for s in r.stops),
            solve_time_ms=(perf_counter() - started) * 1000,
        )
        result = PlanResult(
            generated_at=datetime.now(UTC),
            solver=solver,
            routes=output,
            unassigned=unassigned,
            decisions=decisions,
            metrics=metrics,
            diagnostics={
                "optimization_policy": self.request.optimization_policy.value,
                "urgency_policy": self.request.urgency_policy.value,
                "urgent_start_policy": (
                    "legacy" if self.legacy_objective else self.request.urgent_start_policy.value
                ),
                "objective_order": self.objective_order(),
                "objective_value": list(self.key(routes)),
                "baseline_objective_value": list(baseline_key) if baseline_key is not None else None,
                "route_evaluations": self.evaluator.evaluations,
                "schedule_cache": self.evaluator.stats,
                "accepted_moves": self.moves,
                "urgent_priority_threshold": 80,
                "service_priority_policy": self.request.service_priority_policy.value,
                "emergency_replan_policy": self.request.emergency_replan_policy.value,
                "service_priority_counts": {
                    label: {
                        "total": sum(service_priority_rank(job) == rank for job in self.evaluator.jobs.values()),
                        "assigned": sum(
                            service_priority_rank(job) == rank and job.id in assigned
                            for job in self.evaluator.jobs.values()
                        ),
                    }
                    for rank, label in ((0, "emergency"), (1, "connection"), (2, "repair_or_additional"), (3, "other"))
                },
                "distance_scope": "remaining" if self.replanning else "whole_shift",
                "routing_cache": getattr(self.evaluator.routing, "stats", None),
                "optimality_proven": False,
            },
        )
        attach_previous_diff(self.request, result)
        if self.progress is not None:
            self.progress.update(force=True, phase="validation")
        errors = await validate_plan(self.request, result, self.evaluator.routing)
        if errors:
            raise RuntimeError("Plan validation failed: " + ", ".join(errors))
        result.diagnostics["validation"] = "passed"
        result.metrics.solve_time_ms = round((perf_counter() - started) * 1000, 1)
        return result


class BaselineSolver(Solver):
    def __init__(self, routing: RoutingProvider) -> None:
        self.routing = routing

    async def solve(self, request: PlanRequest) -> PlanResult:
        request = PlanRequest.model_validate(request.model_dump())
        if request.district_mode == DistrictMode.strict:
            from app.solvers.districts import solve_by_district

            return (await solve_by_district(self, request, (request.optimization_policy,)))[request.optimization_policy]
        return await self._solve_flat(request)

    async def _solve_flat_policies(self, request, policies):
        return {p: await self._solve_flat(request.model_copy(update={"optimization_policy": p}))
                for p in policies}

    async def _solve_flat(self, request: PlanRequest) -> PlanResult:
        started = perf_counter()
        request = PlanRequest.model_validate(request.model_dump())
        await self.routing.prepare(request)
        search = Search(request, self.routing)
        if search.progress is not None:
            search.progress.update(force=True, phase="search", stage="fifo")
        routes = await search.baseline()
        if search.progress is not None:
            search.progress.update(force=True, completed_candidates=1)
        return await search.result(routes, "fifo-baseline-v1", started, search.key(routes))


class InsertionSolver(Solver):
    def __init__(
        self, routing: RoutingProvider, *, search_time_limit_ms: float | None = None,
        clock: Callable[[], float] = perf_counter,
        workers: int = 1, parallel_min_jobs: int = 200,
        search_strategy: str = "full", search_attempt_limit: int = 60_000,
        search_slice_attempts: int = 2_000,
    ) -> None:
        if search_time_limit_ms is not None and (
            not isfinite(search_time_limit_ms) or search_time_limit_ms < 0
        ):
            raise ValueError("search_time_limit_ms must be finite and nonnegative")
        from app.solvers.adaptive import nonnegative_integer

        if search_strategy not in {"full", "adaptive"}:
            raise ValueError("search_strategy must be full or adaptive")
        self.search_strategy = search_strategy
        self.search_attempt_limit = nonnegative_integer(
            search_attempt_limit, "search_attempt_limit",
        )
        self.search_slice_attempts = nonnegative_integer(
            search_slice_attempts, "search_slice_attempts", minimum=1,
        )
        self.routing = routing
        self.search_time_limit_ms = search_time_limit_ms
        self.clock = clock
        if isinstance(workers, bool) or not isinstance(workers, int) or not 0 <= workers <= 3:
            raise ValueError("workers must be an integer between 0 (auto) and 3")
        if not isinstance(parallel_min_jobs, int) or parallel_min_jobs < 0:
            raise ValueError("parallel_min_jobs must be a nonnegative integer")
        self.workers = workers
        self.parallel_min_jobs = parallel_min_jobs

    @staticmethod
    def _enter(deadline: SearchDeadline | None, stage: str) -> None:
        progress = current_progress.get()
        if progress is not None:
            progress.update(force=True, phase="search", stage=stage, processed_jobs=0, pass_name="")
        if deadline is not None:
            deadline.enter(stage)

    @staticmethod
    def _completed(deadline: SearchDeadline | None, origin: str, routes) -> None:
        progress = current_progress.get()
        if progress is not None:
            progress.update(force=True, completed_candidates=progress.state["completed_candidates"] + 1)
        if deadline is not None:
            deadline.completed(origin, routes)

    async def solve(self, request: PlanRequest) -> PlanResult:
        results = await self.solve_policies(request, (request.optimization_policy,))
        return results[request.optimization_policy]

    async def solve_policies(
        self,
        request: PlanRequest,
        policies: tuple[OptimizationPolicy, ...],
    ) -> dict[OptimizationPolicy, PlanResult]:
        """Build local candidate pools, partitioning strict district requests first."""
        request = PlanRequest.model_validate(request.model_dump())
        policies = tuple(OptimizationPolicy(policy) for policy in policies)
        if not policies or len(policies) != len(set(policies)):
            raise ValueError("policies must contain unique optimization policies")
        if request.district_mode == DistrictMode.strict:
            from app.solvers.districts import solve_by_district

            return await solve_by_district(self, request, policies)
        return await self._solve_flat_policies(request, policies)

    async def _solve_flat_policies(self, request, policies):
        """Internal non-recursive solve; strict eligibility remains enabled."""
        policies = tuple(OptimizationPolicy(policy) for policy in policies)
        if not policies or len(policies) != len(set(policies)):
            raise ValueError("policies must contain unique optimization policies")
        if self.search_strategy == "adaptive":
            from app.solvers.adaptive import solve_adaptive

            # Policy-specific decisions require independent budgets and searches.
            return {
                policy: await solve_adaptive(
                    self, request.model_copy(update={"optimization_policy": policy}),
                ) for policy in policies
            }
        if len(policies) > 1 and self.search_time_limit_ms is not None:
            raise ValueError("a shared policy search does not support search_time_limit_ms")
        started = perf_counter()
        request = PlanRequest.model_validate(request.model_dump())
        await self.routing.prepare(request)
        deadline = (
            SearchDeadline(self.search_time_limit_ms, clock=self.clock)
            if self.search_time_limit_ms is not None else None
        )
        # Sequential goals share one evaluator. Processes have private caches;
        # the parent evaluator builds and independently validates the result.
        evaluator = ScheduleEvaluator(request, self.routing)
        searches = {
            policy: Search(
                request.model_copy(update={"optimization_policy": policy}), self.routing,
                evaluator, deadline=deadline,
            ) for policy in OptimizationPolicy
        }
        legacy_searches = {
            policy: Search(
                request.model_copy(update={"optimization_policy": policy}), self.routing,
                evaluator, deadline=deadline, legacy_objective=True,
            ) for policy in OptimizationPolicy
        }
        search = searches[policies[0]]
        metadata = {"polishing": [], "compound_search": [], "consolidation": [],
                    "ejection_search": [], "repair_search": [], "route_segments": [],
                    "urgent_start_search": [], "or_opt_search": [],
                    "cross_exchange_search": [], "deep_repair_search": [],
                    "stability_search": [], "cyclic_exchange_search": [],
                    "cyclic_reinsertion_search": [], "ruin_recreate_search": [],
                    "priority_repair_search": []}
        from app.solvers.parallel import ParallelCandidates, execution_mode

        stability_enabled = StabilityRepairSearch.enabled(request)
        progress = current_progress.get()
        if progress is not None:
            progress.update(
                force=True, total_candidates=48 if stability_enabled else 45,
            )
        execution = execution_mode(self, request)
        stopped = False
        business_priority_pool: list[tuple[str, dict[str, Schedule]]] = []
        priority_repair_pool: list[tuple[str, dict[str, Schedule]]] = []
        try:
            if deadline is not None:
                deadline.key = search.key
                # Even a zero budget retains executing work. This O(engineers) setup
                # and final validation are mandatory; it does not schedule future jobs.
                deadline.consider(await search.empty(), "fallback")
                evaluator.search_deadline = deadline
            self._enter(deadline, "initialization")
            if execution["workers"] > 1:
                async with ParallelCandidates(request, self.routing, execution["workers"]) as parallel:
                    pool = await self._candidates(
                        legacy_searches, metadata, deadline, parallel, urgent_searches=searches,
                    )
                    execution["processes"] = parallel.report()
                    evaluator.attempts += sum(s["schedule_attempts"] for s in parallel.report())
            else:
                pool = await self._candidates(
                    legacy_searches, metadata, deadline, urgent_searches=searches,
                )
            if request.service_priority_policy == ServicePriorityPolicy.organizer:
                # Most requests already cover every job, so avoid another full
                # construction unless the historical pool actually assigns a lower
                # business tier while leaving a higher one out.
                priority_policies = [policy for policy in OptimizationPolicy if policy in policies]
                for policy in priority_policies:
                    preliminary = min(pool, key=lambda item: searches[policy].key(item[1]))[1]
                    if not searches[policy].has_service_priority_conflict(preliminary):
                        continue
                    if progress is not None:
                        progress.update(force=True, total_candidates=progress.state["total_candidates"] + 1)
                    self._enter(deadline, policy.value + ":business_priority")
                    routes = await searches[policy].construct("priority")
                    origin = policy.value + ":business_priority"
                    business_priority_pool.append((origin, routes))
                    self._completed(deadline, origin, routes)
                # Fix all seeds before running any policy, keeping shared results
                # independent of the order in which policies were requested.
                priority_seeds = {
                    policy: min(pool + business_priority_pool,
                                key=lambda item: searches[policy].key(item[1]))
                    for policy in priority_policies
                }
                for policy, (seed_origin, seed) in priority_seeds.items():
                    if not searches[policy].has_service_priority_conflict(seed):
                        continue
                    if progress is not None:
                        progress.update(force=True, total_candidates=progress.state["total_candidates"] + 1)
                    origin = policy.value + ":priority_repair"
                    self._enter(deadline, origin)
                    routes, report = await searches[policy].candidate("priority_repair", seed)
                    metadata["priority_repair_search"].append({**report, "seed_origin": seed_origin})
                    priority_repair_pool.append((origin, routes))
                    self._completed(deadline, origin, routes)
            self._enter(deadline, "selection")
        except SearchTimeLimitReached:
            assert deadline is not None
            stopped = True
            pool = deadline.interrupted_pool()
            business_priority_pool = []
            priority_repair_pool = []
        finally:
            evaluator.search_deadline = None  # Final validation must always run.
            if deadline is not None:
                # The bound key method owns Search, which owns this deadline.
                # Break that cycle on success, interruption, error and cancellation
                # so request-scoped caches do not wait for cyclic GC to be freed.
                deadline.key = None
        budget_report = deadline.report(stopped) if deadline is not None else None
        common_final_started = perf_counter()
        baseline = (
            await search.baseline() if deadline is None else deadline.baseline
        )
        accepted_moves = sum(
            s.moves for group in (legacy_searches, searches) for s in group.values()
        )
        worker_evaluations = sum(
            stats["route_evaluations"] for stats in execution.get("processes", [])
        )
        shared_parent_evaluations = evaluator.evaluations
        shared_parent_cache = deepcopy(evaluator.stats)
        shared_execution = deepcopy(execution) | {
            "shared_candidate_pool": len(policies) > 1,
            "result_policies": [item.value for item in policies],
        }
        if len(policies) > 1:
            shared_execution["shared_search"] = {
                "route_evaluations": shared_parent_evaluations + worker_evaluations,
                "parent_route_evaluations": shared_parent_evaluations,
                "schedule_cache": shared_parent_cache,
            }
        results = {}
        for result_number, policy in enumerate(policies, 1):
            progress = current_progress.get()
            if progress is not None and len(policies) > 1:
                progress.update(
                    force=True,
                    phase="finalization",
                    stage=f"result:{policy.value}",
                    result_policy=policy.value,
                    result_number=result_number,
                    total_results=len(policies),
                )
            if len(policies) == 1:
                selected = searches[policy]
            else:
                variant = request.model_copy(update={"optimization_policy": policy})
                selected = Search(
                    variant,
                    self.routing,
                    ScheduleEvaluator(variant, self.routing),
                )
            selection_pool = pool + business_priority_pool + priority_repair_pool
            origin, best = min(selection_pool, key=lambda item: selected.key(item[1]))
            baseline_key = selected.key(baseline) if baseline is not None else None
            result = await selected.result(
                best, "insertion-v19", started, baseline_key, search_stopped=stopped,
            )
            result.diagnostics["accepted_moves"] = accepted_moves
            result.diagnostics.update(deepcopy(metadata))
            result_execution = deepcopy(shared_execution)
            if len(policies) > 1:
                finalization_evaluations = result.diagnostics["route_evaluations"]
                finalization_cache = deepcopy(result.diagnostics["schedule_cache"])
                result_execution["result_finalization"] = {
                    "optimization_policy": policy.value,
                    "route_evaluations": finalization_evaluations,
                    "schedule_cache": finalization_cache,
                }
                # The top-level cache remains a parent-process summary, as in a
                # single solve, while execution exposes its two constituent scopes.
                result.diagnostics["schedule_cache"] = {
                    key: shared_parent_cache.get(key, 0) + finalization_cache.get(key, 0)
                    for key in shared_parent_cache
                }
            result.diagnostics["execution"] = result_execution
            result.diagnostics["route_evaluations"] += worker_evaluations + (
                shared_parent_evaluations if len(policies) > 1 else 0
            )
            result.diagnostics["selected_candidate"] = origin
            result.diagnostics["candidate_pool"] = [
                {"origin": label, "objective_value": list(selected.key(routes)),
                 "assigned_jobs": sum(len(r.visits) for r in routes.values())}
                for label, routes in pool
            ]
            result.diagnostics["business_priority_candidates"] = [
                {"origin": label, "objective_value": list(selected.key(routes)),
                 "assigned_jobs": sum(len(r.visits) for r in routes.values())}
                for label, routes in business_priority_pool
            ]
            result.diagnostics["priority_repair_candidates"] = [
                {"origin": label, "objective_value": list(selected.key(routes)),
                 "assigned_jobs": sum(len(r.visits) for r in routes.values())}
                for label, routes in priority_repair_pool
            ]
            if budget_report is not None:
                report = dict(budget_report)
                report["finalization_ms"] = round(
                    (perf_counter() - common_final_started) * 1000, 2
                )
                result.diagnostics["search_budget"] = report
            results[policy] = result
        if len(policies) > 1:
            shared_solve_time_ms = round((perf_counter() - started) * 1000, 1)
            shared_routing_cache = deepcopy(getattr(self.routing, "stats", None))
            for result in results.values():
                result.metrics.solve_time_ms = shared_solve_time_ms
                # Both validations use the same request-owned routing cache. Report
                # the final common snapshot instead of an order-dependent prefix.
                result.diagnostics["routing_cache"] = deepcopy(shared_routing_cache)
        return results

    async def _candidates(
        self, searches, metadata: dict, deadline, parallel=None, *, urgent_searches,
    ):
        pool: list[tuple[str, dict[str, Schedule]]] = []

        async def run(items, record=None, *, candidate_searches=None):
            # Each item has a fixed seed selected before starting the batch.
            active = searches if candidate_searches is None else candidate_searches
            if parallel is not None:
                results = await parallel.run(items)
                if record is not None:
                    for item, result in zip(items, results):
                        record(item[0], result[0], result[1])
            else:
                results = []
                for policy, stage, seed in items:
                    label = policy.value + (":" + stage if stage != "initial" else "")
                    self._enter(deadline, label)
                    routes, diagnostics = await active[policy].candidate(stage, seed)
                    results.append((routes, diagnostics, 0))
                    self._completed(deadline, label, routes)
                    if record is not None:
                        record(policy, routes, diagnostics)
            for (policy, _, _), (_, _, moves) in zip(items, results):
                active[policy].moves += moves
            return results

        policies = list(searches)
        # Construct all goals concurrently; only SLA's initial improvement needs
        # the staff result. Preserve its original seed-versus-construction ties.
        built = (await parallel.run([(p, "construction", None) for p in policies], complete=False)
                 if parallel is not None else None)

        def initial_item(index, warm_start=None):
            if built is None:
                return policies[index], "initial", warm_start
            return policies[index], "initial_finish", (built[index][0], built[index][1], warm_start)

        initial = await run([initial_item(0), initial_item(1)])
        pool.extend((p.value, result[0]) for p, result in zip(policies[:2], initial))
        sla = policies[2]
        result = (await run([initial_item(2, pool[0][1])]))[0]
        pool.append((sla.value, result[0]))

        for stage, diagnostic_key in (
            ("refined", None), ("polished", "polishing"),
            ("compound", "compound_search"), ("consolidated", "consolidation"),
            ("ejection", "ejection_search"),
            ("repair", "repair_search"),
            ("segments", "route_segments"),
        ):
            # All three goals see the same pool; completed siblings are added only
            # at the barrier, in policy order, regardless of process completion order.
            seeds = [min(pool, key=lambda item: searches[p].key(item[1])) for p in policies]

            def record(policy, routes, diagnostics, *, key=diagnostic_key,
                       stage=stage, seeds=seeds):
                if key is not None:
                    metadata[key].append({
                        "origin": policy.value + ":" + stage,
                        "seed_origin": seeds[policies.index(policy)][0], **diagnostics,
                    })

            results = await run([(p, stage, seed) for p, (_, seed) in zip(policies, seeds)], record)
            for policy, (routes, _, _) in zip(policies, results):
                label = policy.value + ":" + stage
                pool.append((label, routes))
        # Preserve all 24 complete v10 candidates. The changed urgency objective
        # must not erase coverage found before emergency-start optimization.
        seeds = [min(pool, key=lambda item: urgent_searches[p].key(item[1])) for p in policies]

        def record_urgent(policy, routes, diagnostics):
            metadata["urgent_start_search"].append({
                "origin": policy.value + ":urgent",
                "seed_origin": seeds[policies.index(policy)][0], **diagnostics,
            })

        results = await run(
            [(p, "urgent", seed) for p, (_, seed) in zip(policies, seeds)],
            record_urgent, candidate_searches=urgent_searches,
        )
        pool.extend((p.value + ":urgent", routes) for p, (routes, _, _) in zip(policies, results))

        # Keep every complete v11 candidate, then explore a new atomic block
        # neighborhood under the selected v11 objective. The original 27 remain
        # available for final selection, so this stage cannot regress their score.
        seeds = [min(pool, key=lambda item: urgent_searches[p].key(item[1])) for p in policies]

        def record_or_opt(policy, routes, diagnostics):
            metadata["or_opt_search"].append({
                "origin": policy.value + ":or_opt",
                "seed_origin": seeds[policies.index(policy)][0], **diagnostics,
            })

        results = await run(
            [(p, "or_opt", seed) for p, seed in zip(policies, seeds)],
            record_or_opt, candidate_searches=urgent_searches,
        )
        pool.extend((p.value + ":or_opt", routes) for p, (routes, _, _) in zip(policies, results))

        # Preserve all 30 v12 candidates. CROSS-exchange swaps short internal
        # blocks atomically, covering cases where both routes are full and the
        # one-way Or-opt move cannot be made.
        seeds = [min(pool, key=lambda item: urgent_searches[p].key(item[1])) for p in policies]

        def record_cross_exchange(policy, routes, diagnostics):
            metadata["cross_exchange_search"].append({
                "origin": policy.value + ":cross_exchange",
                "seed_origin": seeds[policies.index(policy)][0], **diagnostics,
            })

        results = await run(
            [(p, "cross_exchange", seed) for p, seed in zip(policies, seeds)],
            record_cross_exchange, candidate_searches=urgent_searches,
        )
        pool.extend(
            (p.value + ":cross_exchange", routes)
            for p, (routes, _, _) in zip(policies, results)
        )

        # Preserve all 33 v13 candidates. Deep repair extends the atomic ejection
        # chain by one displaced visit, covering capacity plateaus that require
        # four complete assignments to change together.
        seeds = [min(pool, key=lambda item: urgent_searches[p].key(item[1])) for p in policies]

        def record_deep_repair(policy, routes, diagnostics):
            metadata["deep_repair_search"].append({
                "origin": policy.value + ":deep_repair",
                "seed_origin": seeds[policies.index(policy)][0], **diagnostics,
            })

        results = await run(
            [(p, "deep_repair", seed) for p, seed in zip(policies, seeds)],
            record_deep_repair, candidate_searches=urgent_searches,
        )
        pool.extend(
            (p.value + ":deep_repair", routes)
            for p, (routes, _, _) in zip(policies, results)
        )
        # V15 keeps every v14 candidate unchanged. Stability-enabled residual
        # requests add a projection of the published plan for each objective;
        # ordinary morning plans and explicit full recalculation remain at 36.
        if StabilityRepairSearch.enabled(searches[policies[0]].request):
            seeds = [min(pool, key=lambda item: urgent_searches[p].key(item[1]))
                     for p in policies]

            def record_stability(policy, routes, diagnostics):
                metadata["stability_search"].append({
                    "origin": policy.value + ":stability_repair",
                    **diagnostics,
                })

            results = await run(
                [(p, "stability_repair", seed) for p, seed in zip(policies, seeds)],
                record_stability, candidate_searches=urgent_searches,
            )
            pool.extend(
                (p.value + ":stability_repair", routes)
                for p, (routes, _, _) in zip(policies, results)
            )

        # Preserve the complete v15 pool: 36 ordinary candidates and, when
        # enabled, three residual stability projections. A cyclic exchange then
        # rotates one visit across three routes atomically, escaping plateaus on
        # which every pair exchange is worse or infeasible.
        seeds = [min(pool, key=lambda item: urgent_searches[p].key(item[1])) for p in policies]

        def record_cyclic_exchange(policy, routes, diagnostics):
            metadata["cyclic_exchange_search"].append({
                "origin": policy.value + ":cyclic_exchange",
                "seed_origin": seeds[policies.index(policy)][0], **diagnostics,
            })

        results = await run(
            [(p, "cyclic_exchange", seed) for p, seed in zip(policies, seeds)],
            record_cyclic_exchange, candidate_searches=urgent_searches,
        )
        pool.extend(
            (p.value + ":cyclic_exchange", routes)
            for p, (routes, _, _) in zip(policies, results)
        )

        # Preserve the complete v16 pool: 39 ordinary candidates or 42 when
        # residual stability is enabled. Reinsert every incoming member of a
        # three-route cycle at any position, so a useful assignment rotation is
        # not hidden by the outgoing visits' original indices.
        seeds = [min(pool, key=lambda item: urgent_searches[p].key(item[1])) for p in policies]

        def record_cyclic_reinsertion(policy, routes, diagnostics):
            metadata["cyclic_reinsertion_search"].append({
                "origin": policy.value + ":cyclic_reinsertion",
                "seed_origin": seeds[policies.index(policy)][0], **diagnostics,
            })

        results = await run(
            [(p, "cyclic_reinsertion", seed) for p, seed in zip(policies, seeds)],
            record_cyclic_reinsertion, candidate_searches=urgent_searches,
        )
        pool.extend(
            (p.value + ":cyclic_reinsertion", routes)
            for p, (routes, _, _) in zip(policies, results)
        )
        # V18 keeps the entire v17 pool and all original seed barriers intact.
        # Destroy/repair may temporarily lose coverage internally, but only a
        # complete restoration with a strictly better key can be committed.
        seeds = [min(pool, key=lambda item: urgent_searches[p].key(item[1])) for p in policies]

        def record_ruin_recreate(policy, routes, diagnostics):
            metadata["ruin_recreate_search"].append({
                "origin": policy.value + ":ruin_recreate",
                "seed_origin": seeds[policies.index(policy)][0], **diagnostics,
            })

        results = await run(
            [(p, "ruin_recreate", seed) for p, seed in zip(policies, seeds)],
            record_ruin_recreate, candidate_searches=urgent_searches,
        )
        pool.extend(
            (p.value + ":ruin_recreate", routes)
            for p, (routes, _, _) in zip(policies, results)
        )
        return pool
