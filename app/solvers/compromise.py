"""Bounded local SLA search under hard limits relative to a validated reference.

The reference is always an incumbent, including with zero attempts. Generators
retain their enumeration positions across fixed-size slices, so a tight limit
cannot restart one unproductive prefix forever. Only complete, independently
schedulable plans may be committed; a half-evaluated swap is never published.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING
from time import perf_counter

from app.domain.compromise import CompromiseOptions
from app.domain.models import PlanRequest, PlanResult, UrgentStartPolicy
from app.routing.base import RoutingProvider
from app.solvers.adaptive import AttemptController, AttemptLimitReached, StageSliceReached
from app.solvers.compromise_moves import CompoundCompromiseMoves
from app.solvers.deadline import SearchDeadline, SearchTimeLimitReached
from app.solvers.insertion import Search
from app.solvers.scheduling import Schedule

Routes = dict[str, Schedule]
FLOAT_TOLERANCE = 1e-9
SLICE_ATTEMPTS = 2000
MAX_ROUNDS = 6
BASIC_PREFIX_ATTEMPTS = 30000


@dataclass(frozen=True)
class Summary:
    assigned_jobs: int
    used_engineers: int
    distance_km: float
    sla_met_jobs: int
    late_minutes: float
    urgent_start_minutes: float
    soft_cost: float


class CompromiseSearch(Search):
    def __init__(self, request: PlanRequest, routing: RoutingProvider) -> None:
        super().__init__(request, routing)
        self.reference_ids: frozenset[str] = frozenset()
        self.reference: Summary | None = None
        self.max_distance = 0.0
        self.max_used = 0
        self.target: int | None = None
        self.options = CompromiseOptions()
        self.best: Routes = {}
        self.best_key: tuple = ()
        self.sla_jobs = sum(j.sla_deadline is not None for j in self.evaluator.jobs.values())
        self.sla_assigned = 0
        self.trace: list[dict] = []
        self.move_log: list[dict] = []
        self.control: AttemptController | None = None
        self.preserve_urgent = request.urgent_start_policy in {
            UrgentStartPolicy.before_primary, UrgentStartPolicy.before_coverage,
        }

    def summarize(self, routes: Routes) -> Summary:
        # Fixed engineer order and full sums avoid subtract/add drift at caps.
        values = [routes[eid] for eid in self.ids]
        return Summary(
            sum(len(r.visits) for r in values), sum(bool(r.visits) for r in values),
            sum(r.distance for r in values),
            self.sla_assigned - sum(r.sla_late_jobs for r in values),
            sum(r.late_minutes for r in values),
            sum(r.urgent_start_minutes for r in values), sum(r.soft_cost for r in values),
        )

    def initialize(self, routes: Routes, options: CompromiseOptions) -> None:
        self.options = options
        self.reference_ids = frozenset().union(*(r.assigned_job_ids for r in routes.values()))
        self.sla_assigned = sum(
            self.evaluator.jobs[jid].sla_deadline is not None for jid in self.reference_ids
        )
        self.reference = self.summarize(routes)
        self.max_distance = self.reference.distance_km * (1 + options.extra_distance_percent / 100)
        self.max_used = min(len(self.ids), self.reference.used_engineers + options.extra_engineers)
        if options.target_sla_percent is not None:
            self.target = int((
                Decimal(str(options.target_sla_percent)) * self.sla_jobs / 100
            ).to_integral_value(rounding=ROUND_CEILING))
        self.best = dict(routes)
        self.best_key = self.key(routes)
        self._reference_key = self.best_key
        if self.progress is not None:
            self.progress.committed(routes)
        if self.best_key[0]:
            raise RuntimeError("The reference must satisfy its own compromise limits")

    def violations(self, routes: Routes, value: Summary) -> bool:
        assert self.reference is not None
        assigned = frozenset().union(*(r.assigned_job_ids for r in routes.values()))
        return (
            assigned != self.reference_ids
            or value.assigned_jobs != len(self.reference_ids)
            or value.used_engineers > self.max_used
            or value.distance_km > self.max_distance + FLOAT_TOLERANCE
            or value.sla_met_jobs < self.reference.sla_met_jobs
            or value.late_minutes > self.reference.late_minutes + FLOAT_TOLERANCE
            or (self.preserve_urgent and value.urgent_start_minutes
                > self.reference.urgent_start_minutes + FLOAT_TOLERANCE)
        )

    def key(self, routes: Routes) -> tuple:
        value = self.summarize(routes)
        invalid = int(self.violations(routes, value))
        if self.target is None:
            return (invalid, -value.sla_met_jobs, value.late_minutes, value.used_engineers,
                    value.distance_km, value.urgent_start_minutes, value.soft_cost)
        deficit = max(0, self.target - value.sla_met_jobs)
        return (invalid, deficit, value.late_minutes if deficit else 0.0,
                value.used_engineers, value.distance_km, -value.sla_met_jobs,
                value.late_minutes, value.urgent_start_minutes, value.soft_cost)

    def objective_order(self) -> list[str]:
        if self.target is None:
            return ["constraint_violation", "negative_sla_met_jobs", "late_minutes",
                    "used_engineers", "distance_km", "urgent_start_minutes", "soft_cost"]
        return ["constraint_violation", "target_sla_deficit", "late_minutes_until_target",
                "used_engineers", "distance_km", "negative_sla_met_jobs", "late_minutes",
                "urgent_start_minutes", "soft_cost"]

    def consider(self, routes: Routes, stage: str) -> bool:
        key = self.key(routes)
        if key[0] or key >= self.best_key:
            return False
        # No generator mutates Schedule or a yielded route dictionary afterwards.
        self.best, self.best_key = dict(routes), key
        self.moves += 1
        if len(self.move_log) < 128:
            self.move_log.append({"stage": stage, "attempt": self.control.used if self.control else 0,
                                  "objective_value": list(key)})
        if self.progress is not None:
            self.progress.committed(routes)
        return True

    def order_visits(self) -> list[str]:
        visits = {v.job.id: v for r in self.best.values() for v in r.visits if not v.executing}
        # Explicit priorities and deterministic IDs, not hash/set iteration.
        def rank(jid: str) -> tuple:
            v = visits[jid]
            late = max(0.0, (v.start - v.job.sla_deadline).total_seconds()) if v.job.sla_deadline else 0
            waiting = (v.start - v.arrival).total_seconds()
            return (not bool(late), -late, -waiting, -v.km, jid)
        return sorted(visits, key=rank)

    async def relocations(self) -> AsyncIterator[Routes | None]:
        for jid in self.order_visits():
            await asyncio.sleep(0)
            self.check_time()
            snapshot = dict(self.best)
            owner = next(eid for eid in self.ids if jid in snapshot[eid].jobs)
            source = snapshot[owner]
            reduced, _ = await self.evaluator.evaluate(
                owner, tuple(j for j in source.jobs if j != jid),
            )
            # Yield even on failure to let another operator use the next slice.
            yield None
            if reduced is None:
                continue  # Deletion is not guaranteed feasible on a non-metric matrix.
            trial = {**snapshot, owner: reduced}
            for eid in self.ids:
                if self.evaluator.eligibility(self.by_id[jid], self.evaluator.engineers[eid]):
                    continue
                old = trial[eid]
                # A published plan can include deliberate extra waiting. Full
                # evaluation, not earliest-only insertion pruning, is required.
                for pos in range(len(old.jobs) + 1):
                    self.check_time()
                    candidate, _ = await self.evaluator.evaluate(
                        eid, old.jobs[:pos] + (jid,) + old.jobs[pos:],
                    )
                    yield {**trial, eid: candidate} if candidate is not None else None

    async def swaps(self) -> AsyncIterator[Routes | None]:
        order = self.order_visits()
        for left_index, left_job in enumerate(order):
            await asyncio.sleep(0)
            self.check_time()
            snapshot = dict(self.best)
            owners = {jid: eid for eid in self.ids for jid in snapshot[eid].jobs}
            left_id = owners[left_job]
            left = snapshot[left_id]
            i = left.jobs.index(left_job)
            for right_job in order[left_index + 1:]:
                self.check_time()
                right_id = owners[right_job]
                if self.evaluator.eligibility(self.by_id[left_job], self.evaluator.engineers[right_id]):
                    continue
                if self.evaluator.eligibility(self.by_id[right_job], self.evaluator.engineers[left_id]):
                    continue
                right = snapshot[right_id]
                j = right.jobs.index(right_job)
                left_jobs = list(left.jobs)
                left_jobs[i] = right_job
                if left_id == right_id:
                    left_jobs[j] = left_job
                changed_left, _ = await self.evaluator.evaluate(left_id, tuple(left_jobs))
                if changed_left is None:
                    yield None
                    continue
                if left_id == right_id:
                    yield {**snapshot, left_id: changed_left}
                    continue
                right_jobs = list(right.jobs)
                right_jobs[j] = left_job
                changed_right, _ = await self.evaluator.evaluate(right_id, tuple(right_jobs))
                yield ({**snapshot, left_id: changed_left, right_id: changed_right}
                       if changed_right is not None else None)

    async def run_portfolio(self, factories: list[tuple[str, Callable[[], AsyncIterator[Routes | None]]]], phase: str) -> str:
        """Run a resumable portfolio; all phases share the same global counter."""
        assert self.control is not None
        generators: list[tuple[str, AsyncIterator]] = []
        try:
            for round_number in range(1, MAX_ROUNDS + 1):
                self.rounds += 1
                before = self.moves
                generators = [(name, factory()) for name, factory in factories]
                while generators:
                    for stage, generator in list(generators):
                        start_attempts, start_moves = self.control.used, self.moves
                        if self.progress is not None:
                            self.progress.update(force=True, phase="search", stage="compromise:" + stage,
                                                 budget_attempts=self.control.used,
                                                 budget_remaining=self.control.limit - self.control.used)
                        exhausted = False
                        try:
                            while self.control.used - start_attempts < SLICE_ATTEMPTS:
                                self.check_time()
                                candidate = await anext(generator)
                                if candidate is not None:
                                    self.consider(candidate, stage)
                        except StopAsyncIteration:
                            exhausted = True
                            generators.remove((stage, generator))
                        finally:
                            self.trace.append({"round": round_number, "phase": phase, "stage": stage,
                                               "schedule_attempts": self.control.used - start_attempts,
                                               "accepted_moves": self.moves - start_moves,
                                               "enumeration_completed": exhausted})
                if self.moves == before:
                    return "neighborhood_exhausted"
            return "round_limit"
        finally:
            for _, generator in generators:
                await generator.aclose()

    async def optimize_reference(self, *, time_limit_ms: float | None = None) -> dict:
        # Keep the incumbent and original reference, not per-run counters. Assign
        # fresh lists so reports returned by earlier runs remain unchanged.
        self.moves = self.rounds = 0
        self.trace = []
        self.move_log = []
        self.control = AttemptController(self.options.attempt_limit, self)
        self.evaluator.attempt_control = self.control
        if time_limit_ms is not None:
            self.deadline = SearchDeadline(time_limit_ms)
            self.evaluator.search_deadline = self.deadline
        started = perf_counter()
        basic_prefix = None
        basic = [("relocation", self.relocations), ("swap", self.swaps)]
        try:
            if not self.sla_jobs:
                stop_reason = "no_sla_jobs"
            elif self.options.neighborhood == "basic":
                stop_reason = await self.run_portfolio(basic, "basic")
            else:
                # Preserve the default v20 baseline before spending on larger
                # neighborhoods. This fixed threshold is NOT scaled by total
                # budget: increasing only the budget keeps a deterministic prefix.
                self.control.begin("basic_prefix", BASIC_PREFIX_ATTEMPTS)
                prefix_reason = "interrupted"
                try:
                    prefix_reason = await self.run_portfolio(basic, "basic_prefix")
                except StageSliceReached:
                    prefix_reason = "prefix_limit"
                except AttemptLimitReached:
                    prefix_reason = "attempt_limit"
                    raise
                except SearchTimeLimitReached:
                    prefix_reason = "time_limit"
                    raise
                finally:
                    basic_prefix = {
                        "max_attempts": min(self.options.attempt_limit, BASIC_PREFIX_ATTEMPTS),
                        "schedule_attempts": self.control.used,
                        "stop_reason": prefix_reason,
                        "objective_value": list(self.best_key),
                        "summary": asdict(self.summarize(self.best)),
                    }
                    self.control.begin("compound_polish", None)
                compound = CompoundCompromiseMoves(self)
                stop_reason = await self.run_portfolio(
                    [("free_swap", compound.free_swaps),
                     ("tail_exchange", compound.tail_exchanges), *basic], "compound_polish")
        except AttemptLimitReached:
            stop_reason = "attempt_limit"
        except SearchTimeLimitReached:
            stop_reason = "time_limit"
        finally:
            self.evaluator.attempt_control = None
            self.evaluator.search_deadline = None
            self.deadline = None
        value = self.summarize(self.best)
        assert self.reference is not None
        return {
            "neighborhood": self.options.neighborhood,
            "basic_prefix": basic_prefix,
            "basic_prefix_preserved": (None if basic_prefix is None else
                                       self.best_key <= tuple(basic_prefix["objective_value"])),
            "operators": self.operator_summary(),
            "mode": "maximize_sla_under_limits" if self.target is None else "target_sla_then_resources",
            "reference": asdict(self.reference), "selected": asdict(value),
            "options": self.options.model_dump(),
            "limits": {"max_distance_km": self.max_distance, "max_used_engineers": self.max_used,
                       "min_sla_met_jobs": self.reference.sla_met_jobs,
                       "max_late_minutes": self.reference.late_minutes,
                       "max_urgent_start_minutes": (self.reference.urgent_start_minutes
                                                    if self.preserve_urgent else None)},
            "sla_jobs": self.sla_jobs, "target_sla_jobs": self.target,
            "target_reached": None if self.target is None or not self.sla_jobs else value.sla_met_jobs >= self.target,
            "stop_reason": stop_reason, "schedule_attempts": self.control.used,
            "search_elapsed_ms": round((perf_counter() - started) * 1000, 2),
            "rounds": self.rounds, "accepted_moves": self.moves, "stages": self.trace,
            "moves": self.move_log, "move_log_truncated": self.moves > len(self.move_log),
            "same_assigned_jobs": True, "optimality_proven": False,
            "unassigned_search": "excluded_from_compromise",
            "budget_scope": "search_schedule_attempts_including_cache_hits; reference_validation_preparation_and_finalization_excluded",
            "distance_tolerance_km": FLOAT_TOLERANCE,
        }

    def operator_summary(self) -> list[dict]:
        """Account for every schedule attempt, including partially evaluated moves."""
        names = ["relocation", "swap"]
        if self.options.neighborhood == "extended":
            names += ["free_swap", "tail_exchange"]
        return [{"stage": name,
                 "schedule_attempts": sum(t["schedule_attempts"] for t in self.trace
                                          if t["stage"] == name),
                 "accepted_moves": sum(t["accepted_moves"] for t in self.trace
                                       if t["stage"] == name),
                 "slices": sum(t["stage"] == name for t in self.trace)} for name in names]

    async def build_result(self, reference_plan: PlanResult, started: float, report: dict) -> PlanResult:
        result = await self.result(
            self.best, "insertion-compromise-v21", started, self.key_reference,
            unassigned_scope=(
                "compromise_scope",
                "Заявка не была назначена в исходном плане. Поиск компромисса сохраняет "
                "тот же набор назначенных работ и не ищет места для остальных. "
                "Это не доказательство невозможности назначения.",
            ),
        )
        message = ("План выбран по SLA внутри заданных ограничений пробега и числа инженеров; "
                   "сохранён исходный набор назначенных заявок.")
        if self.target is not None:
            message += " После достижения целевого SLA приоритет отдан меньшему числу инженеров и пробегу."
        for route in result.routes:
            for stop in route.stops:
                # Remove a plain policy claim: this is a different, constrained objective.
                stop.explanation = [text for text in stop.explanation if not text.startswith((
                    "при одинаковом покрытии", "после обязательных закреплений",
                    "проверены позиции вставки",
                ))]
                stop.explanation.append(message)
        result.diagnostics["compromise"] = report
        result.diagnostics["plan_label"] = "Компромисс по SLA"
        result.diagnostics["reference_solver"] = reference_plan.solver
        result.diagnostics["execution"] = {"workers": 1, "shared_candidate_pool": False,
                                             "reference_reused": True}
        return result

    @property
    def key_reference(self) -> tuple:
        return self._reference_key
