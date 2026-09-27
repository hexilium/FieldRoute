"""Resumable compound moves for the resource-constrained SLA search.

Only complete two-route plans are yielded. Temporary route removal is never
used as a feasibility test: with a directed/non-metric matrix that would prune
valid exchanges. Resource caps are tested only on the final combined plan.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from app.solvers.scheduling import Schedule

if TYPE_CHECKING:
    from app.solvers.compromise import CompromiseSearch

Routes = dict[str, Schedule]


class CompoundCompromiseMoves:
    def __init__(self, search: CompromiseSearch) -> None:
        self.search = search

    def eligible(self, jid: str, eid: str) -> bool:
        evaluator = self.search.evaluator
        return not evaluator.eligibility(self.search.by_id[jid], evaluator.engineers[eid])

    async def free_swaps(self) -> AsyncIterator[Routes | None]:
        """Exchange two jobs and try every receiving position, not just old slots.

        A snapshot stays fixed for one outer job. Later incumbent improvements
        never mutate it. Each receiving schedule is evaluated by the common
        evaluator, so cache hits and the second half of an exchange also consume
        the global attempt budget. No cross-product is materialized in memory.
        """
        search = self.search
        order = search.order_visits()
        for index, left_job in enumerate(order):
            await asyncio.sleep(0)
            search.check_time()
            snapshot = dict(search.best)
            owners = {jid: eid for eid in search.ids for jid in snapshot[eid].jobs}
            left_id = owners[left_job]
            left = snapshot[left_id].jobs
            old_left_pos = left.index(left_job)
            for right_job in order[index + 1:]:
                await asyncio.sleep(0)
                search.check_time()
                right_id = owners[right_job]
                if left_id == right_id:
                    continue
                right = snapshot[right_id].jobs
                if len(left) == len(right) == 1:
                    continue  # The basic swap already covers the only positions.
                if not self.eligible(left_job, right_id) or not self.eligible(right_job, left_id):
                    continue
                old_right_pos = right.index(right_job)
                reduced_left = tuple(j for j in left if j != left_job)
                reduced_right = tuple(j for j in right if j != right_job)
                for i in range(len(reduced_left) + 1):
                    search.check_time()
                    new_left, _ = await search.evaluator.evaluate(
                        left_id, reduced_left[:i] + (right_job,) + reduced_left[i:],
                    )
                    # An evaluation without a full plan must still yield control
                    # to the slice scheduler. It must never become an incumbent.
                    yield None
                    if new_left is None:
                        continue
                    for j in range(len(reduced_right) + 1):
                        search.check_time()
                        if i == old_left_pos and j == old_right_pos:
                            continue  # Already covered by the fixed-position swap.
                        new_right, _ = await search.evaluator.evaluate(
                            right_id, reduced_right[:j] + (left_job,) + reduced_right[j:],
                        )
                        yield ({**snapshot, left_id: new_left, right_id: new_right}
                               if new_right is not None else None)

    def first_eligible_cut(self, jobs: tuple[str, ...], target: str) -> int:
        """Any suffix containing an incompatible job can be safely excluded."""
        for i in range(len(jobs) - 1, -1, -1):
            self.search.check_time()
            if not self.eligible(jobs[i], target):
                return i + 1
        return 0

    async def tail_exchanges(self) -> AsyncIterator[Routes | None]:
        """Exchange ordered suffixes, including transfers to/from an empty tail.

        Every final route is checked in full. There is no symmetric-distance,
        triangle-inequality, or 'deleting a stop is always feasible' assumption.
        Executing visits are fixed evaluator prefixes, not movable job tuples.
        """
        search = self.search

        def rank(eid: str) -> tuple:
            r = search.best[eid]
            return (-r.sla_late_jobs, -r.late_minutes, -r.distance, eid)

        order = sorted(search.ids, key=rank)
        for index, left_id in enumerate(order):
            for right_id in order[index + 1:]:
                await asyncio.sleep(0)
                search.check_time()
                snapshot = dict(search.best)
                left, right = snapshot[left_id].jobs, snapshot[right_id].jobs
                first_left = self.first_eligible_cut(left, right_id)
                first_right = self.first_eligible_cut(right, left_id)
                for i in range(first_left, len(left) + 1):
                    for j in range(first_right, len(right) + 1):
                        search.check_time()
                        if i == len(left) and j == len(right):
                            continue
                        # One-stop relocations and one-to-one tail swaps are
                        # already in the basic portfolio. The compound operator
                        # spends its attempts on genuinely different moves.
                        if len(left) - i <= 1 and len(right) - j <= 1:
                            continue
                        new_left, _ = await search.evaluator.evaluate(left_id, left[:i] + right[j:])
                        if new_left is None:
                            yield None
                            continue
                        new_right, _ = await search.evaluator.evaluate(right_id, right[:j] + left[i:])
                        yield ({**snapshot, left_id: new_left, right_id: new_right}
                               if new_right is not None else None)
