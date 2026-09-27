"""Request-local, deterministic allocation of schedule attempts.

This is an optional anytime heuristic, not a replacement for the full candidate
pool. Only committed feasible schedules reach the incumbent. Operator selection
uses observed lexicographic improvements and attempt counts, never elapsed time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from time import perf_counter
from typing import TYPE_CHECKING

from app.domain.models import OptimizationPolicy, PlanRequest, ServicePriorityPolicy
from app.solvers.deadline import SearchDeadline, SearchTimeLimitReached
from app.solvers.scheduling import Schedule, ScheduleEvaluator
from app.solvers.stability_search import StabilityRepairSearch

if TYPE_CHECKING:
    from app.solvers.insertion import InsertionSolver, Search


class AttemptLimitReached(Exception):
    """The request has used its complete schedule-attempt allowance."""


class StageSliceReached(Exception):
    """One operator must yield the remaining request budget to its peers."""


def nonnegative_integer(value: int, name: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def signature(routes: dict[str, Schedule]) -> tuple:
    # Retimed residual schedules can share job order but differ in start times.
    return tuple((eid, route.jobs, tuple(v.start for v in route.visits))
                 for eid, route in routes.items())


class AttemptController:
    """Count before cache lookup, pruning or evaluation, with no off-by-one work.

    Exhaustion is checked before the NEXT attempt, not after the last allowed
    one: a feasible improvement from the final attempt may still be committed.
    """
    def __init__(self, limit: int, search: Search) -> None:
        self.limit = nonnegative_integer(limit, "search_attempt_limit")
        self.search = search
        self.used = 0
        self.slice_limit: int | None = None
        self.slice_start = 0
        self.stage = "fallback"
        self.best: dict[str, Schedule] | None = None
        self.best_key: tuple | None = None
        self.best_origin = "fallback"
        self.checkpoints = 0

    def begin(self, stage: str, allowance: int | None) -> None:
        self.stage, self.slice_limit, self.slice_start = stage, allowance, self.used

    def take(self) -> None:
        if self.used >= self.limit:
            raise AttemptLimitReached
        if self.slice_limit is not None and self.used - self.slice_start >= self.slice_limit:
            raise StageSliceReached
        self.used += 1

    def consider(self, routes: dict[str, Schedule], detail: str = "checkpoint") -> None:
        self.checkpoints += 1
        key = self.search.key(routes)
        if self.best_key is None or key < self.best_key:
            self.best = dict(routes)  # FIFO mutates its dictionary, not Schedule values.
            self.best_key = key
            self.best_origin = f"adaptive:{self.stage}:{detail}"


@dataclass
class OperatorStats:
    calls: int = 0
    attempts: int = 0
    improvements: int = 0
    reward: Fraction = field(default_factory=Fraction)
    # Per complete seed, remember the requested slice, not time-dependent work.
    tried: dict[tuple, tuple[int, bool]] = field(default_factory=dict)


class OperatorPortfolio:
    """Probe every operator, then favour useful ones with periodic exploration.

    A truncated, non-improving attempt may retry the same seed only with a larger
    slice (2x, at most 8x the base). An exhausted operator is not replayed on the
    identical seed. A changed seed makes operators eligible again.
    """
    def __init__(self, stages: list[str], slice_attempts: int) -> None:
        self.base = nonnegative_integer(slice_attempts, "search_slice_attempts", minimum=1)
        self.stages = list(stages)
        if not stages or len(set(stages)) != len(stages):
            raise ValueError("stages must be nonempty and unique")
        self.stats = {stage: OperatorStats() for stage in stages}
        self.selections = 0

    def choose(self, seed: tuple) -> tuple[str, int, str] | None:
        eligible = []
        for index, stage in enumerate(self.stages):
            stats = self.stats[stage]
            previous = stats.tried.get(seed)
            if previous is None:
                allowance = self.base
            elif previous[1] and previous[0] < self.base * 8:
                allowance = previous[0] * 2
            else:
                continue
            eligible.append((index, stage, allowance))
        if not eligible:
            return None
        unseen = [item for item in eligible if not self.stats[item[1]].calls]
        if unseen:
            chosen, reason = unseen[0], "first_probe"
        elif self.selections % 4 == 0:
            chosen = min(eligible, key=lambda x: (self.stats[x[1]].calls, x[0]))
            reason = "exploration"
        else:
            # Exact fractions avoid float ties. No incompatible objective units
            # are added: reward depends only on the FIRST improved component.
            chosen = min(eligible, key=lambda x: (
                -(self.stats[x[1]].reward + Fraction(1, 16))
                / max(self.base, self.stats[x[1]].attempts),
                self.stats[x[1]].calls, x[0],
            ))
            reason = "observed_improvement_per_attempt"
        self.selections += 1
        return chosen[1], chosen[2], reason

    def record(self, stage: str, seed: tuple, allowance: int, attempts: int,
               before: tuple, after: tuple, *, truncated: bool) -> None:
        stats = self.stats[stage]
        stats.calls += 1
        stats.attempts += attempts
        stats.tried[seed] = allowance, truncated
        if after < before:
            first = next(i for i, (old, new) in enumerate(zip(before, after)) if old != new)
            stats.improvements += 1
            stats.reward += Fraction(1, first + 1)

    def report(self) -> list[dict]:
        return [{"stage": stage, "calls": s.calls, "schedule_attempts": s.attempts,
                 "improvements": s.improvements, "distinct_seeds": len(s.tried),
                 "reward": str(s.reward)} for stage, s in self.stats.items()]


async def solve_adaptive(solver: InsertionSolver, request: PlanRequest):
    # Imported lazily to keep Search usable by individual neighborhood tests.
    from app.solvers.insertion import Search
    from app.solvers.parallel import execution_mode

    started = perf_counter()
    request = PlanRequest.model_validate(request.model_dump())
    await solver.routing.prepare(request)
    deadline = (SearchDeadline(solver.search_time_limit_ms, clock=solver.clock)
                if solver.search_time_limit_ms is not None else None)
    evaluator = ScheduleEvaluator(request, solver.routing)
    search = Search(request, solver.routing, evaluator, deadline=deadline)
    control = AttemptController(solver.search_attempt_limit, search)
    trace: list[dict] = []
    pool: list[tuple[str, dict[str, Schedule]]] = []
    baseline = None
    stop_reason = "portfolio_exhausted"
    stages = ["refined", "polished", "compound", "consolidated", "ejection", "repair",
              "segments", "or_opt", "cross_exchange", "deep_repair", "cyclic_exchange",
              "cyclic_reinsertion", "ruin_recreate"]
    if StabilityRepairSearch.enabled(request):
        stages.insert(0, "stability_repair")
    if request.service_priority_policy == ServicePriorityPolicy.organizer:
        stages.insert(0, "priority_repair")
    portfolio = OperatorPortfolio(stages, solver.search_slice_attempts)
    max_rounds = 128
    progress = search.progress
    if progress is not None:
        progress.update(force=True, search_strategy="adaptive", total_candidates=133,
                        attempt_limit=control.limit, budget_attempts=0, completed_candidates=0)

    async def run(stage: str, allowance: int | None, action, selection: str):
        control.begin(stage, allowance)
        before = control.best_key
        before_attempts = control.used
        stage_started = perf_counter()
        outcome, details = "completed", None
        try:
            solver._enter(deadline, "adaptive:" + stage)
            routes, details = await action()
            control.consider(routes, "completed")
            return routes, details
        except StageSliceReached:
            outcome = "slice_limit"
            return control.best, None
        except AttemptLimitReached:
            outcome = "attempt_limit"
            raise
        except SearchTimeLimitReached:
            outcome = "time_limit"
            raise
        except BaseException:
            outcome = "error_or_cancelled"
            raise  # Never turn cancellation or an implementation error into a plan.
        finally:
            trace.append({
                "stage": stage, "selection": selection, "allowance": allowance,
                "schedule_attempts": control.used - before_attempts,
                "total_attempts": control.used, "stop_reason": outcome,
                "improved": control.best_key < before,
                "initial_objective_value": list(before),
                "objective_value": list(control.best_key),
                "elapsed_ms": round((perf_counter() - stage_started) * 1000, 3),
                "details": details,
            })
            pool.append((control.best_origin, dict(control.best)))
            if progress is not None:
                progress.update(force=True, completed_candidates=len(trace),
                                budget_attempts=control.used,
                                budget_remaining=control.limit - control.used)

    async def fifo():
        nonlocal baseline
        routes = await search.baseline()
        baseline = dict(routes)
        return routes, None

    async def construct(mode):
        return await search.construct(mode), None

    try:
        # Mandatory bootstrap retains executing work even with a zero allowance.
        # It is O(engineers), outside both search budgets, as in the full solver.
        empty = await search.empty()
        search_started = perf_counter()
        control.consider(empty, "fallback")
        pool.append((control.best_origin, dict(empty)))
        search.attempt_control = evaluator.attempt_control = control
        if deadline is not None:
            deadline.key = search.key
            deadline.consider(empty, "fallback")
            evaluator.search_deadline = deadline
        await run("fifo", None, fifo, "baseline")
        modes = ["scarcity", "deadline", "priority"]
        if request.optimization_policy == OptimizationPolicy.sla_first:
            modes.append("sla")
        for mode in modes:
            if control.used >= control.limit:
                raise AttemptLimitReached
            await run("construct:" + mode, portfolio.base,
                      lambda mode=mode: construct(mode), "construction_probe")
        for _ in range(max_rounds):
            if control.used >= control.limit:
                raise AttemptLimitReached
            seed_signature = signature(control.best)
            choice = portfolio.choose(seed_signature)
            if choice is None:
                break
            stage, allowance, selection = choice
            before = control.best_key
            seed = dict(control.best)
            if stage in {"or_opt", "cross_exchange", "deep_repair", "cyclic_exchange",
                         "cyclic_reinsertion", "ruin_recreate", "stability_repair"}:
                # This is a single-policy portfolio; avoid fictitious cross-policy polishing.
                seed = (request.optimization_policy.value + ":adaptive", seed)
            try:
                await run(stage, allowance,
                          lambda stage=stage, seed=seed: search.candidate(stage, seed), selection)
            finally:
                item = trace[-1]
                native_limit = (item["details"] or {}).get("stop_reason") in {
                    "attempt_limit", "trial_attempt_limit", "node_limit", "probe_limit",
                }
                portfolio.record(stage, seed_signature, allowance, item["schedule_attempts"],
                                 before, control.best_key,
                                 truncated=item["stop_reason"] == "slice_limit" or native_limit)
        else:
            stop_reason = "round_limit"
    except AttemptLimitReached:
        stop_reason = "attempt_limit"
    except SearchTimeLimitReached:
        stop_reason = "time_limit"
    finally:
        # Finalization/independent validation cannot be starved by the optimizer.
        search.attempt_control = evaluator.attempt_control = None
        evaluator.search_deadline = None
        if deadline is not None:
            deadline.key = None
        if progress is not None:
            progress.update(force=True, total_candidates=len(trace),
                            completed_candidates=len(trace))

    search_ms = (perf_counter() - search_started) * 1000
    search_attempts = evaluator.attempts
    reason_code = {"time_limit": "search_time_limit", "attempt_limit": "search_attempt_limit"}.get(
        stop_reason, "search_portfolio_limit",
    )
    budget_report = deadline.report(stop_reason == "time_limit") if deadline else None
    final_started = perf_counter()
    result = await search.result(
        control.best, "insertion-v19", started,
        search.key(baseline) if baseline is not None else None,
        search_stopped=True, search_stop_reason=reason_code,
    )
    result.diagnostics.update({
        "selected_candidate": control.best_origin,
        "candidate_pool": [{"origin": label, "objective_value": list(search.key(routes)),
                            "assigned_jobs": sum(len(r.visits) for r in routes.values())}
                           for label, routes in pool],
        "execution": execution_mode(solver, request) | {"shared_candidate_pool": False},
        "adaptive_search": {
            "strategy": "adaptive", "attempt_limit": control.limit,
            "schedule_attempts": control.used, "slice_attempts": portfolio.base,
            "max_slice_attempts": portfolio.base * 8, "max_rounds": max_rounds,
            "stop_reason": stop_reason, "baseline_completed": baseline is not None,
            "baseline_guaranteed": baseline is not None,
            "checkpoints": control.checkpoints, "stages": trace,
            "operators": portfolio.report(),
            "search_elapsed_ms": round(search_ms, 3),
            "finalization_ms": round((perf_counter() - final_started) * 1000, 3),
            "bootstrap_attempts": search_attempts - control.used,
            "finalization_attempts": evaluator.attempts - search_attempts,
            "repeatability_guaranteed": deadline is None,
            "repeatability_scope": "same input, routing data, code, settings and environment",
            "cross_run_coverage_guaranteed": False,
            "full_pool_preserved": False,
            "unassigned_probes": "skipped_in_bounded_search",
            "scope": "schedule attempts including cache hits and bounds; bootstrap, routing "
                     "preparation, finalization and independent validation excluded",
        },
    })
    if deadline is not None:
        result.diagnostics["search_budget"] = budget_report | {
            "stop_reason": stop_reason, "completed_candidates": len(trace),
            "baseline_completed": baseline is not None,
            "baseline_guaranteed": baseline is not None,
            "unassigned_probes": "skipped_in_bounded_search",
            "finalization_ms": result.diagnostics["adaptive_search"]["finalization_ms"],
        }
    return result
