"""Bounded, deterministic destroy/beam-repair with atomic publication.

A temporary loss of coverage is internal search state, never a published plan.
Unlike single improving moves, rebuilding several visits can cross a plateau.
No external optimizer, random generator or extra dependency is required.
"""

from __future__ import annotations

import asyncio
from bisect import insort
from itertools import zip_longest
from typing import TYPE_CHECKING

from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class _LimitReached(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason


class RuinRecreateSearch:
    """Rebuild small related groups, retaining every incumbent assignment.

    All placements use complete scheduling, including on retimed residual seeds.
    The beam stores only a bounded number of alternatives. Completed candidates
    are checked immediately, before another budget check can interrupt the trial.
    """

    stage = "ruin_recreate"

    def __init__(
        self, search: Search, budget: SearchBudget, *, removal_sizes=(4, 3, 2),
        beam_width: int = 4, max_trials: int = 64, max_trial_attempts: int = 2000,
        max_nodes: int = 2000, max_probes: int = 1024,
    ) -> None:
        for name, value, lower in (
            ("schedule attempt limit", budget.limit, 0),
            ("beam width", beam_width, 1), ("trial limit", max_trials, 0),
            ("trial attempt limit", max_trial_attempts, 1),
            ("node limit", max_nodes, 0), ("probe limit", max_probes, 0),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < lower:
                raise ValueError(f"{name} must be an integer >= {lower}")
        sizes = tuple(removal_sizes)
        if not sizes or len(set(sizes)) != len(sizes) or any(
            isinstance(n, bool) or not isinstance(n, int) or not 2 <= n <= 4 for n in sizes
        ):
            raise ValueError("removal_sizes must contain unique integers from 2 to 4")
        self.search, self.budget = search, budget
        self.removal_sizes, self.beam_width = sizes, beam_width
        self.max_trials, self.max_trial_attempts = max_trials, max_trial_attempts
        self.max_nodes, self.max_probes = max_nodes, max_probes
        self.nodes = self.probes = self.trials = self.completed_repairs = 0
        self.limited_trials = self.infeasible_ruins = 0
        self._trial_start = 0
        self.routes: dict[str, Schedule] = {}
        self.moves: list[dict] = []

    async def cooperate(self) -> None:
        self.search.check_time()
        if self.search.progress is not None:
            self.search.progress.update()
        await asyncio.sleep(0)
        self.search.check_time()

    async def evaluate(self, engineer_id: str, jobs: tuple[str, ...]) -> Schedule | None:
        self.search.check_time()
        if self.budget.attempts >= self.budget.limit:
            raise _LimitReached("attempt_limit")
        if self.budget.attempts - self._trial_start >= self.max_trial_attempts:
            raise _LimitReached("trial_attempt_limit")
        self.budget.take()
        route, _ = await self.search.evaluator.evaluate(engineer_id, jobs)
        return route

    def groups(self, routes: dict[str, Schedule]):
        """Interleave route, costly-leg and geographic proposals lazily.

        Coordinate proximity is only a proposal heuristic, not a travel estimate
        or feasibility bound. Every surviving candidate uses the real provider.
        Frozen/locked jobs and the executing prefix are never removed.
        """
        search = self.search
        visits = {
            visit.job.id: visit for route in routes.values() for visit in route.visits
            if not visit.executing and not search.evaluator.forced[visit.job.id]
        }
        if len(visits) < min(self.removal_sizes):
            return
        present = set().union(*(r.assigned_job_ids for r in routes.values()))
        missing = search.order([j for j in search.jobs if j.id not in present], "scarcity")
        # Input IDs give deterministic ties without relying on set iteration.
        expensive = sorted(visits, key=lambda jid: (-visits[jid].km, jid))

        def costly():
            for size in self.removal_sizes:
                for index in range(max(0, len(expensive) - size + 1)):
                    yield "costly_legs", tuple(expensive[index:index + size])

        def consecutive():
            for size in self.removal_sizes:
                for route in routes.values():
                    for index in range(max(0, len(route.jobs) - size + 1)):
                        group = route.jobs[index:index + size]
                        if all(jid in visits for jid in group):
                            yield "route_segment", group

        def related():
            anchors = [*missing, *(search.by_id[jid] for jid in expensive)]
            for anchor in anchors:
                nearby = sorted(visits, key=lambda jid: (
                    (search.by_id[jid].location.lat - anchor.location.lat) ** 2
                    + (search.by_id[jid].location.lon - anchor.location.lon) ** 2,
                    jid,
                ))
                for size in self.removal_sizes:
                    if len(nearby) >= size:
                        yield "related", tuple(nearby[:size])

        for batch in zip_longest(costly(), consecutive(), related()):
            for proposal in batch:
                if proposal is not None:
                    yield proposal

    def consider(self, candidate, preserved, info) -> None:
        """Only a complete restoration may reach a checkpoint or incumbent."""
        assigned = set().union(*(route.assigned_job_ids for route in candidate.values()))
        if not preserved <= assigned:
            raise AssertionError("an incomplete ruin/repair cannot be committed")
        self.completed_repairs += 1
        before, after = self.search.key(self.routes), self.search.key(candidate)
        if after < before:
            self.routes = dict(candidate)
            self.moves.append({
                **info, "initial_objective_value": list(before),
                "objective_value": list(after),
                "restored_jobs": list(info["removed_jobs"]),
                "added_jobs": sorted(assigned - preserved),
            })
            self.search.moves += 1
            self.search.checkpoint(self.routes, self.stage)

    async def repair(self, base, pending, preserved, info) -> None:
        """Beam search in a fixed job order; all placements compete per layer.

        No partial plan is checkpointed. We intentionally permit worse partial
        objectives and retain alternative placements to avoid a greedy dead end.
        """
        search = self.search
        beam = [base]
        for depth, job in enumerate(pending):
            choices = []
            seen = set()
            ordinal = 0
            final = depth == len(pending) - 1
            for partial in beam:
                if self.nodes >= self.max_nodes:
                    raise _LimitReached("node_limit")
                self.nodes += 1
                await self.cooperate()
                for eid in search.ids:
                    search.check_time()
                    if search.evaluator.eligibility(job, search.evaluator.engineers[eid]):
                        continue
                    old = partial[eid]
                    for position in range(len(old.jobs) + 1):
                        sequence = old.jobs[:position] + (job.id,) + old.jobs[position:]
                        changed = await self.evaluate(eid, sequence)
                        if changed is None:
                            continue
                        candidate = {**partial, eid: changed}
                        if final:
                            self.consider(candidate, preserved, info)
                            continue
                        signature = tuple(candidate[k].jobs for k in search.ids)
                        if signature in seen:
                            continue
                        seen.add(signature)
                        insort(choices, (search.key(candidate), ordinal, candidate))
                        ordinal += 1
                        if len(choices) > self.beam_width:
                            choices.pop()
            if final or not choices:
                return
            beam = [candidate for _, _, candidate in choices]

    async def trial(self, snapshot, group, strategy, missing, order) -> None:
        self.trials += 1
        self._trial_start = self.budget.attempts
        await self.cooperate()
        removed = frozenset(group)
        preserved = set().union(*(r.assigned_job_ids for r in snapshot.values()))
        base = dict(snapshot)
        for eid, route in snapshot.items():
            if removed.intersection(route.jobs):
                changed = await self.evaluate(eid, tuple(j for j in route.jobs if j not in removed))
                if changed is None:
                    # On a directed/non-metric matrix deletion may be infeasible.
                    # Reject this proposal, never fabricate a feasible empty leg.
                    self.infeasible_ruins += 1
                    return
                base[eid] = changed
        pending = self.search.order([self.search.by_id[jid] for jid in group], "scarcity")
        if order == "reverse":
            pending.reverse()
        if missing is not None:
            pending.insert(0, missing)
        info = {
            "strategy": strategy, "removed_jobs": sorted(group), "repair_order": order,
            "target_job": missing.id if missing else None,
            "pending_order": [job.id for job in pending],
        }
        await self.repair(base, pending, preserved, info)

    async def improve(self, routes: dict[str, Schedule]) -> tuple[dict[str, Schedule], dict]:
        self.routes = dict(routes)
        initial = self.search.key(routes)
        passes = 0
        reason = "neighborhood_exhausted"
        try:
            while True:
                self.search.check_time()
                if self.budget.attempts >= self.budget.limit:
                    raise _LimitReached("attempt_limit")
                if self.trials >= self.max_trials:
                    raise _LimitReached("trial_limit")
                passes += 1
                before_moves = len(self.moves)
                snapshot = self.routes
                present = set().union(*(r.assigned_job_ids for r in snapshot.values()))
                missing = self.search.order(
                    [j for j in self.search.jobs if j.id not in present], "scarcity",
                )
                seen = set()
                for strategy, group in self.groups(snapshot):
                    self.search.check_time()
                    if self.probes >= self.max_probes:
                        raise _LimitReached("probe_limit")
                    self.probes += 1
                    if self.probes % 32 == 0:
                        await self.cooperate()
                    signature = tuple(sorted(group))
                    if signature in seen:
                        continue
                    seen.add(signature)
                    # Coverage repair gets first access to the budget. Pure
                    # reshuffling is still tried on the same set if it fails.
                    targets = [*missing[:2], None]
                    for target in targets:
                        for order in ("scarcity", "reverse"):
                            if self.trials >= self.max_trials:
                                raise _LimitReached("trial_limit")
                            try:
                                await self.trial(snapshot, group, strategy, target, order)
                            except _LimitReached as exc:
                                if exc.reason != "trial_attempt_limit":
                                    raise
                                self.limited_trials += 1
                            if len(self.moves) > before_moves:
                                break
                        if len(self.moves) > before_moves:
                            break
                    if len(self.moves) > before_moves:
                        break
                if len(self.moves) == before_moves:
                    if self.limited_trials:
                        reason = "trial_attempt_limit"
                    break
        except _LimitReached as exc:
            reason = exc.reason
        return self.routes, {
            "optimization_policy": self.search.request.optimization_policy.value,
            "urgent_start_policy": self.search.request.urgent_start_policy.value,
            "objective_order": self.search.objective_order(),
            "initial_objective_value": list(initial),
            "objective_value": list(self.search.key(self.routes)),
            "schedule_attempts": self.budget.attempts, "max_schedule_attempts": self.budget.limit,
            "removal_sizes": list(self.removal_sizes), "beam_width": self.beam_width,
            "trials": self.trials, "max_trials": self.max_trials,
            "max_trial_attempts": self.max_trial_attempts,
            "nodes": self.nodes, "max_nodes": self.max_nodes,
            "probes": self.probes, "max_probes": self.max_probes,
            "completed_repairs": self.completed_repairs, "limited_trials": self.limited_trials,
            "infeasible_ruins": self.infeasible_ruins,
            "passes": passes, "stop_reason": reason,
            "accepted_moves": len(self.moves), "moves": self.moves,
        }
