"""Bounded atomic two-route exchanges with free reinsertion positions."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from itertools import product
from typing import TYPE_CHECKING, Any

from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.insertion import Search, SearchBudget


class _BudgetExhausted(Exception):
    pass


class _ProbeExhausted(Exception):
    pass


class _PositionCombinationExhausted(Exception):
    pass


class PairedReinsertionSearch:
    """Swap one visit between two routes and freely reinsert both visits.

    ``CrossExchangeSearch`` replaces each outgoing block at the other block's
    original position.  That misses exchanges where the new owner is correct
    but either incoming visit belongs elsewhere in its receiving route.  This
    neighborhood removes one visit from each route, checks every reinsertion
    position, and exposes only the completed two-route exchange.
    """

    stage = "paired_reinsertion"

    def __init__(
        self,
        search: Search,
        budget: SearchBudget,
        *,
        max_probes: int = 100_000,
        max_position_combinations: int = 100_000,
    ) -> None:
        if (
            isinstance(budget.limit, bool)
            or not isinstance(budget.limit, int)
            or budget.limit < 0
        ):
            raise ValueError("schedule attempt limit must be a nonnegative integer")
        if (
            isinstance(max_probes, bool)
            or not isinstance(max_probes, int)
            or max_probes < 0
        ):
            raise ValueError("probe limit must be a nonnegative integer")
        if (
            isinstance(max_position_combinations, bool)
            or not isinstance(max_position_combinations, int)
            or max_position_combinations < 0
        ):
            raise ValueError(
                "position combination limit must be a nonnegative integer"
            )
        self.search, self.budget = search, budget
        self.max_probes = max_probes
        self.probes = 0
        self.max_position_combinations = max_position_combinations
        self.position_combinations = 0

    async def check_probe(self) -> None:
        """Bound outgoing-pair enumeration and keep it cancellable."""
        self.search.check_time()
        if self.probes >= self.max_probes:
            raise _ProbeExhausted
        self.probes += 1
        if self.probes % 128 == 0:
            if self.search.progress is not None:
                self.search.progress.update()
            await asyncio.sleep(0)

    async def check_position_combination(self) -> None:
        """Bound residual Cartesian scoring without changing probe accounting."""
        self.search.check_time()
        if self.position_combinations >= self.max_position_combinations:
            raise _PositionCombinationExhausted
        self.position_combinations += 1
        if self.position_combinations % 128 == 0:
            if self.search.progress is not None:
                self.search.progress.update()
            await asyncio.sleep(0)

    async def evaluate(self, engineer_id: str, jobs: tuple[str, ...]) -> Schedule | None:
        self.search.check_time()
        if not self.budget.take():
            raise _BudgetExhausted
        route, _ = await self.search.evaluator.evaluate(engineer_id, jobs)
        return route

    def eligible(self, job_id: str, engineer_id: str) -> bool:
        evaluator = self.search.evaluator
        return not evaluator.eligibility(
            self.search.by_id[job_id], evaluator.engineers[engineer_id],
        )

    async def feasible_reinsertions(
        self,
        routes: dict[str, Schedule],
        engineer_id: str,
        outgoing_position: int,
        incoming_job_id: str,
    ) -> list[tuple[int, Schedule]]:
        """Evaluate every feasible position for one side of a fixed swap."""
        old = routes[engineer_id]
        reduced = old.jobs[:outgoing_position] + old.jobs[outgoing_position + 1 :]
        options = []
        for insertion_position in range(len(reduced) + 1):
            jobs = (
                reduced[:insertion_position]
                + (incoming_job_id,)
                + reduced[insertion_position:]
            )
            route = await self.evaluate(engineer_id, jobs)
            if route is not None:
                options.append((insertion_position, route))
        return options

    async def best_reinsertion(
        self,
        routes: dict[str, Schedule],
        engineer_id: str,
        outgoing_position: int,
        incoming_job_id: str,
    ) -> tuple[int, Schedule] | None:
        """Return the best ordinary-plan position for one fixed incoming visit.

        For a fixed swap, coverage and route usage do not depend on insertion
        position.  Ranking only the changed route avoids losing small route
        differences when a large unchanged float is added to both alternatives.
        """
        best: tuple[int, Schedule] | None = None
        best_key: tuple | None = None
        for insertion_position, route in await self.feasible_reinsertions(
            routes,
            engineer_id,
            outgoing_position,
            incoming_job_id,
        ):
            option_key = self.search.key({engineer_id: route})
            if best_key is None or option_key < best_key:
                best = insertion_position, route
                best_key = option_key
        return best

    async def best_residual_combination(
        self,
        routes: dict[str, Schedule],
        engineer_ids: tuple[str, str],
        choices: list[list[tuple[int, Schedule]]],
    ) -> tuple[dict[str, Schedule], list[int]]:
        """Rank the full Cartesian product with the authoritative residual key."""
        best_candidate: dict[str, Schedule] | None = None
        best_positions: list[int] | None = None
        best_key: tuple | None = None
        for combination in product(*choices):
            await self.check_position_combination()
            positions = [position for position, _route in combination]
            changed = {
                engineer_id: route
                for engineer_id, (_position, route) in zip(
                    engineer_ids,
                    combination,
                    strict=True,
                )
            }
            candidate = {**routes, **changed}
            candidate_key = self.search.key(candidate)
            if best_key is None or candidate_key < best_key:
                best_candidate = candidate
                best_positions = positions
                best_key = candidate_key
        assert best_candidate is not None and best_positions is not None
        return best_candidate, best_positions

    async def exchange(
        self,
        routes: dict[str, Schedule],
        engineer_ids: tuple[str, str],
        outgoing_positions: tuple[int, int],
        outgoing_job_ids: tuple[str, str],
    ) -> tuple[dict[str, Schedule], dict[str, Any]] | None:
        """Evaluate every reinsertion position before exposing a full swap."""
        left_id, right_id = engineer_ids
        left_job, right_job = outgoing_job_ids
        incoming_job_ids = right_job, left_job
        if not (
            self.eligible(right_job, left_id)
            and self.eligible(left_job, right_id)
        ):
            return None

        if self.search.replanning:
            choices = []
            for engineer_id, outgoing_position, incoming_job_id in zip(
                engineer_ids,
                outgoing_positions,
                incoming_job_ids,
                strict=True,
            ):
                options = await self.feasible_reinsertions(
                    routes,
                    engineer_id,
                    outgoing_position,
                    incoming_job_id,
                )
                if not options:
                    return None
                choices.append(options)
            candidate, insertion_positions = await self.best_residual_combination(
                routes,
                engineer_ids,
                choices,
            )
        else:
            changed: dict[str, Schedule] = {}
            insertion_positions = []
            for engineer_id, outgoing_position, incoming_job_id in zip(
                engineer_ids,
                outgoing_positions,
                incoming_job_ids,
                strict=True,
            ):
                result = await self.best_reinsertion(
                    routes,
                    engineer_id,
                    outgoing_position,
                    incoming_job_id,
                )
                if result is None:
                    return None
                insertion_position, route = result
                insertion_positions.append(insertion_position)
                changed[engineer_id] = route
            candidate = {**routes, **changed}
        return candidate, {
            "engineers": list(engineer_ids),
            "outgoing_positions": list(outgoing_positions),
            "insertion_positions": insertion_positions,
            "outgoing_job_ids": list(outgoing_job_ids),
            "incoming_job_ids": list(incoming_job_ids),
            "repositioned_jobs": sum(
                outgoing != incoming
                for outgoing, incoming in zip(
                    outgoing_positions,
                    insertion_positions,
                    strict=True,
                )
            ),
        }

    async def candidates(
        self, routes: dict[str, Schedule],
    ) -> AsyncIterator[tuple[dict[str, Schedule], dict[str, Any]]]:
        search = self.search
        assigned = set().union(*(route.assigned_job_ids for route in routes.values()))
        urgency_sweeps = (True, False) if assigned & search._urgent_ids else (False,)
        for urgent in urgency_sweeps:
            for left_index, left_id in enumerate(search.ids):
                left = routes[left_id]
                if not left.jobs:
                    continue
                for right_id in search.ids[left_index + 1 :]:
                    search.check_time()
                    right = routes[right_id]
                    if not right.jobs:
                        continue
                    if urgent and not (
                        (left.assigned_job_ids | right.assigned_job_ids)
                        & search._urgent_ids
                    ):
                        continue
                    engineer_ids = left_id, right_id
                    for left_position, left_job in enumerate(left.jobs):
                        for right_position, right_job in enumerate(right.jobs):
                            await self.check_probe()
                            outgoing_job_ids = left_job, right_job
                            has_urgent = any(
                                job_id in search._urgent_ids for job_id in outgoing_job_ids
                            )
                            if has_urgent != urgent:
                                continue
                            result = await self.exchange(
                                routes,
                                engineer_ids,
                                (left_position, right_position),
                                outgoing_job_ids,
                            )
                            if result is not None:
                                yield result

    async def improve(
        self, routes: dict[str, Schedule],
    ) -> tuple[dict[str, Schedule], dict[str, Any]]:
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
            reason = "attempt_limit"
        except _ProbeExhausted:
            reason = "probe_limit"
        except _PositionCombinationExhausted:
            reason = "position_combination_limit"
        return routes, {
            "optimization_policy": search.request.optimization_policy.value,
            "urgent_start_policy": search.request.urgent_start_policy.value,
            "objective_order": search.objective_order(),
            "stop_reason": reason,
            "passes": passes,
            "schedule_attempts": self.budget.attempts,
            "max_schedule_attempts": self.budget.limit,
            "probes": self.probes,
            "max_probes": self.max_probes,
            "position_combinations": self.position_combinations,
            "max_position_combinations": self.max_position_combinations,
            "accepted_moves": len(moves),
            "accepted_job_exchanges": 2 * len(moves),
            "accepted_repositioned_jobs": sum(
                move["repositioned_jobs"] for move in moves
            ),
            "moves": moves,
            "initial_objective_value": list(initial_key),
            "objective_value": list(key),
        }
