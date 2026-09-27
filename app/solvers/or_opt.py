"""Bounded relocation of short, ordered route blocks (Or-opt)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class _BudgetExhausted(Exception):
    pass


class OrOptSearch:
    """Relocate two- and three-visit blocks without exposing partial plans.

    Single-visit relocation is already covered by ``Search.polish``. Keeping a
    block's order lets this stage cross local minima without duplicating route
    reversal or tail-exchange neighborhoods from ``RouteSegmentSearch``.
    """

    stage = "or_opt"

    def __init__(
        self, search: Search, budget: SearchBudget, *, max_block_size: int = 3,
    ) -> None:
        if budget.limit < 0:
            raise ValueError("max schedule attempts must be nonnegative")
        if (
            isinstance(max_block_size, bool)
            or not isinstance(max_block_size, int)
            or not 2 <= max_block_size <= 3
        ):
            raise ValueError("max_block_size must be 2 or 3")
        self.search, self.budget = search, budget
        self.max_block_size = max_block_size

    async def evaluate(self, engineer_id: str, jobs: tuple[str, ...]) -> Schedule | None:
        self.search.check_time()
        if not self.budget.take():
            raise _BudgetExhausted
        route, _ = await self.search.evaluator.evaluate(engineer_id, jobs)
        return route

    def blocks(
        self, routes: dict[str, Schedule],
    ) -> list[tuple[str, int, int, tuple[str, ...]]]:
        """List blocks deterministically, considering emergency blocks first."""
        blocks = []
        for source_index, source_id in enumerate(self.search.ids):
            jobs = routes[source_id].jobs
            for size in range(2, self.max_block_size + 1):
                for start in range(len(jobs) - size + 1):
                    block = jobs[start:start + size]
                    urgent = any(job_id in self.search._urgent_ids for job_id in block)
                    blocks.append((source_id, start, source_index, block, urgent))
        blocks.sort(key=lambda item: (not item[4], len(item[3]), item[2], item[1]))
        return [(source_id, start, source_index, block)
                for source_id, start, source_index, block, _ in blocks]

    async def candidates(
        self, routes: dict[str, Schedule],
    ) -> AsyncIterator[tuple[dict[str, Schedule], dict[str, Any]]]:
        search, evaluator = self.search, self.search.evaluator
        for source_id, start, _, block in self.blocks(routes):
            search.check_time()
            source = routes[source_id]
            end = start + len(block)
            reduced_jobs = source.jobs[:start] + source.jobs[end:]

            # Reinsert the block into its current route. The original position is
            # a no-op; every other sequence is checked as one complete schedule.
            for position in range(len(reduced_jobs) + 1):
                if position == start:
                    continue
                jobs = reduced_jobs[:position] + block + reduced_jobs[position:]
                changed = await self.evaluate(source_id, jobs)
                if changed is not None:
                    yield {**routes, source_id: changed}, {
                        "source_engineer": source_id,
                        "target_engineer": source_id,
                        "job_ids": list(block),
                        "from_position": start,
                        "to_position": position,
                    }

            # A cross-route move is atomic. The reduced source is evaluated once
            # per block, but never published unless the receiving route also fits.
            reduced = await self.evaluate(source_id, reduced_jobs)
            if reduced is None:
                continue
            for target_id in search.ids:
                if target_id == source_id:
                    continue
                engineer = evaluator.engineers[target_id]
                if any(evaluator.eligibility(search.by_id[job_id], engineer)
                       for job_id in block):
                    continue
                target = routes[target_id]
                for position in range(len(target.jobs) + 1):
                    jobs = target.jobs[:position] + block + target.jobs[position:]
                    changed = await self.evaluate(target_id, jobs)
                    if changed is not None:
                        yield {**routes, source_id: reduced, target_id: changed}, {
                            "source_engineer": source_id,
                            "target_engineer": target_id,
                            "job_ids": list(block),
                            "from_position": start,
                            "to_position": position,
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
            "block_sizes": list(range(2, self.max_block_size + 1)),
            "accepted_moves": len(moves),
            "accepted_same_route": sum(
                move["source_engineer"] == move["target_engineer"] for move in moves
            ),
            "accepted_cross_route": sum(
                move["source_engineer"] != move["target_engineer"] for move in moves
            ),
            "moves": moves,
            "initial_objective_value": list(initial_key),
            "objective_value": list(key),
        }
