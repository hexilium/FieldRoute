"""Bounded backtracking over insertions when repairing displaced work."""

from __future__ import annotations

from bisect import insort

from app.solvers.ejection import EjectionSearch


class RepairSearch(EjectionSearch):
    """Try alternative placements before abandoning a bounded repair.

    A greedy insertion can occupy the only feasible slot for the next pending
    job. Keep a small, stably ranked set of placements and backtrack if that
    next job cannot be placed. The last pending job still uses ordinary best
    insertion, since no remaining work can be blocked by its choice.
    """

    stage = "repair"

    def __init__(self, search, budget, *, insertion_width=3, **kwargs):
        if (isinstance(insertion_width, bool) or not isinstance(insertion_width, int)
                or insertion_width < 1):
            raise ValueError("insertion_width must be a positive integer")
        super().__init__(search, budget, **kwargs)
        self.insertion_width = insertion_width
        self.branch_points = self.alternative_insertions = 0

    async def insertions(self, routes, job, pending, budget):
        if not pending or self.insertion_width == 1:
            async for direct in super().insertions(routes, job, pending, budget):
                yield direct
            return

        # Imported lazily: Search's stage registry imports this module.
        from app.solvers.insertion import InsertionObjective

        search = self.search
        search.check_time()
        objective = InsertionObjective(search, routes, job)
        choices = []
        ordinal = 0
        for eid in search.ids:
            search.check_time()
            if search.evaluator.eligibility(job, search.evaluator.engineers[eid]):
                continue
            route = routes[eid]
            for position in range(len(route.jobs) + 1):
                if not budget.take():
                    break
                candidate = await search.evaluator.evaluate_insertion(eid, route, job.id, position)
                if candidate is not None:
                    # A unique ordinal preserves engineer/position tie-breaking
                    # and prevents tuple comparison from reaching Schedule objects.
                    insort(choices, (objective.key(eid, candidate), ordinal, eid, candidate))
                    if len(choices) > self.insertion_width:
                        choices.pop()
                ordinal += 1
            if budget.attempts >= budget.limit:
                break

        if len(choices) > 1:
            self.branch_points += 1
        for index, (_, _, eid, candidate) in enumerate(choices):
            search.check_time()
            if index:
                self.alternative_insertions += 1
            yield {**routes, eid: candidate}

    async def improve(self, routes):
        self.branch_points = self.alternative_insertions = 0
        routes, report = await super().improve(routes)
        return routes, {
            **report, "insertion_width": self.insertion_width,
            "branch_points": self.branch_points,
            "alternative_insertions": self.alternative_insertions,
        }
