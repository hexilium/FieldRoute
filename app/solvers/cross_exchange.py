"""Bounded atomic exchanges of short blocks between two routes."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class _BudgetExhausted(Exception):
    pass


class CrossExchangeSearch:
    """Exchange one- or two-visit blocks without exposing half a move.

    The earlier ``CompoundSearch`` also checks one-for-one exchanges, but it runs
    against an older seed and objective. Rechecking them here is necessary after
    urgent-start and Or-opt stages. Larger pairs complement 2-opt* (tail exchange)
    and Or-opt (one-way block relocation) when both routes are full or a one-way
    move is otherwise infeasible.
    """

    stage = "cross_exchange"

    def __init__(
        self, search: Search, budget: SearchBudget, *, max_block_size: int = 2,
    ) -> None:
        if budget.limit < 0:
            raise ValueError("max schedule attempts must be nonnegative")
        if (
            isinstance(max_block_size, bool)
            or not isinstance(max_block_size, int)
            or max_block_size != 2
        ):
            raise ValueError("max_block_size must be 2")
        self.search, self.budget = search, budget
        self.max_block_size = max_block_size
        self.probes = 0

    async def check_probe(self) -> None:
        """Keep deadlines and process cancellation responsive before evaluation."""
        self.search.check_time()
        self.probes += 1
        if self.probes % 128 == 0:
            if self.search.progress is not None:
                self.search.progress.update()
            # Eligibility and capacity filters can otherwise form a long CPU-only
            # loop with no schedule evaluation to provide an async cancellation point.
            await asyncio.sleep(0)

    async def evaluate(self, engineer_id: str, jobs: tuple[str, ...]) -> Schedule | None:
        self.search.check_time()
        if not self.budget.take():
            raise _BudgetExhausted
        route, _ = await self.search.evaluator.evaluate(engineer_id, jobs)
        return route

    def eligible(self, job_ids: tuple[str, ...], engineer_id: str) -> bool:
        evaluator = self.search.evaluator
        engineer = evaluator.engineers[engineer_id]
        return all(not evaluator.eligibility(self.search.by_id[job_id], engineer)
                   for job_id in job_ids)

    async def block_pairs(
        self, routes: dict[str, Schedule], left_id: str, right_id: str,
        *, urgent: bool,
    ) -> AsyncIterator[tuple[int, int, tuple[str, ...], tuple[str, ...]]]:
        """Yield deterministic exchanges, with urgent work in the first sweep."""
        left, right = routes[left_id].jobs, routes[right_id].jobs
        for left_size in range(1, min(self.max_block_size, len(left)) + 1):
            for right_size in range(1, min(self.max_block_size, len(right)) + 1):
                self.search.check_time()
                # A different block size changes the number of jobs on both routes.
                # Reject impossible max_jobs cases before evaluating schedules.
                evaluator = self.search.evaluator
                left_count = (
                    len(left) - left_size + right_size
                    + bool(evaluator.executing.get(left_id))
                )
                right_count = (
                    len(right) - right_size + left_size
                    + bool(evaluator.executing.get(right_id))
                )
                left_limit = evaluator.engineers[left_id].max_jobs
                right_limit = evaluator.engineers[right_id].max_jobs
                if ((left_limit is not None and left_count > left_limit)
                        or (right_limit is not None and right_count > right_limit)):
                    continue
                for left_start in range(len(left) - left_size + 1):
                    await self.check_probe()
                    left_block = left[left_start:left_start + left_size]
                    if not self.eligible(left_block, right_id):
                        continue
                    for right_start in range(len(right) - right_size + 1):
                        await self.check_probe()
                        right_block = right[right_start:right_start + right_size]
                        has_urgent = any(
                            job_id in self.search._urgent_ids
                            for job_id in (*left_block, *right_block)
                        )
                        if has_urgent != urgent or not self.eligible(right_block, left_id):
                            continue
                        # Size loops prefer smaller changes; the remaining loops
                        # preserve route and position order without a quadratic list.
                        yield left_start, right_start, left_block, right_block

    async def candidates(
        self, routes: dict[str, Schedule],
    ) -> AsyncIterator[tuple[dict[str, Schedule], dict[str, Any]]]:
        search = self.search
        # Two sweeps prioritize exchanges involving emergencies without building
        # one quadratic, request-wide descriptor list in memory.
        assigned = set().union(*(route.assigned_job_ids for route in routes.values()))
        urgency_sweeps = (True, False) if assigned & search._urgent_ids else (False,)
        for urgent in urgency_sweeps:
            for left_index, left_id in enumerate(search.ids):
                left = routes[left_id]
                if not left.jobs:
                    continue
                for right_id in search.ids[left_index + 1:]:
                    search.check_time()
                    right = routes[right_id]
                    if not right.jobs:
                        continue
                    if urgent and not (
                        (left.assigned_job_ids | right.assigned_job_ids) & search._urgent_ids
                    ):
                        continue
                    async for left_start, right_start, left_block, right_block in self.block_pairs(
                        routes, left_id, right_id, urgent=urgent,
                    ):
                        left_end = left_start + len(left_block)
                        right_end = right_start + len(right_block)
                        left_jobs = (
                            left.jobs[:left_start] + right_block + left.jobs[left_end:]
                        )
                        right_jobs = (
                            right.jobs[:right_start] + left_block + right.jobs[right_end:]
                        )
                        changed_left = await self.evaluate(left_id, left_jobs)
                        if changed_left is None:
                            continue
                        changed_right = await self.evaluate(right_id, right_jobs)
                        if changed_right is None:
                            continue
                        yield {**routes, left_id: changed_left, right_id: changed_right}, {
                            "left_engineer": left_id,
                            "right_engineer": right_id,
                            "left_job_ids": list(left_block),
                            "right_job_ids": list(right_block),
                            "left_position": left_start,
                            "right_position": right_start,
                        }

    async def improve(self, routes: dict[str, Schedule]) -> tuple[dict[str, Schedule], dict]:
        search = self.search
        initial_key = key = search.key(routes)
        moves: list[dict[str, Any]] = []
        passes = 0
        reason = "attempt_limit"
        try:
            while self.budget.attempts < self.budget.limit:
                passes += 1
                improved = False
                async for candidate, move in self.candidates(routes):
                    candidate_key = search.key(candidate)
                    if candidate_key < key:
                        routes, key = candidate, candidate_key
                        moves.append(move)
                        search.moves += 1
                        search.checkpoint(routes, self.stage)
                        improved = True
                        break
                if not improved:
                    reason = "neighborhood_exhausted"
                    break
        except _BudgetExhausted:
            pass  # Retain the last complete, strictly improving plan.
        return routes, {
            "optimization_policy": search.request.optimization_policy.value,
            "urgent_start_policy": search.request.urgent_start_policy.value,
            "objective_order": search.objective_order(),
            "stop_reason": reason,
            "passes": passes,
            "schedule_attempts": self.budget.attempts,
            "max_schedule_attempts": self.budget.limit,
            "block_sizes": list(range(1, self.max_block_size + 1)),
            "accepted_moves": len(moves),
            "accepted_job_exchanges": sum(
                len(move["left_job_ids"]) + len(move["right_job_ids"])
                for move in moves
            ),
            "moves": moves,
            "initial_objective_value": list(initial_key),
            "objective_value": list(key),
        }
