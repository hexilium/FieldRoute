"""Cooperative search deadline and an incumbent containing only committed routes."""

from __future__ import annotations

from collections.abc import Callable
from math import isfinite
from time import perf_counter
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.solvers.scheduling import Schedule


class SearchTimeLimitReached(Exception):
    pass


class SearchDeadline:
    def __init__(self, limit_ms: float, *, clock: Callable[[], float] = perf_counter) -> None:
        if not isfinite(limit_ms) or limit_ms < 0:
            raise ValueError("search_time_limit_ms must be finite and nonnegative")
        self.limit_ms = limit_ms
        self.clock = clock
        self.started = clock()
        self.deadline = self.started + limit_ms / 1000
        self.stage = "initialization"
        self.best: tuple[str, dict[str, Schedule]] | None = None
        self.best_key: tuple | None = None
        self.key: Callable | None = None
        self.baseline: dict[str, Schedule] | None = None
        self.completed_candidates: list[tuple[str, dict[str, Schedule]]] = []
        self.checkpoints = 0

    def check(self) -> None:
        if self.clock() >= self.deadline:
            raise SearchTimeLimitReached

    def enter(self, stage: str) -> None:
        self.stage = stage
        self.check()

    def consider(self, routes: dict[str, Schedule], detail: str = "checkpoint") -> None:
        key = self.key(routes)
        self.checkpoints += 1
        if self.best_key is None or key < self.best_key:
            # FIFO mutates its routes dictionary in place. Never retain that alias.
            self.best = (f"{self.stage}:{detail}", dict(routes))
            self.best_key = key

    def completed(self, origin: str, routes: dict[str, Schedule]) -> None:
        self.completed_candidates.append((origin, dict(routes)))
        self.consider(routes)

    def interrupted_pool(self) -> list[tuple[str, dict[str, Schedule]]]:
        pool = list(self.completed_candidates)
        if not pool or self.best_key < min(self.key(routes) for _, routes in pool):
            assert self.best is not None
            pool.append(self.best)
        return pool

    def report(self, stopped: bool) -> dict:
        elapsed = max(0.0, (self.clock() - self.started) * 1000)
        return {
            "limit_ms": self.limit_ms,
            "stop_reason": "time_limit" if stopped else "completed",
            "stage": self.stage,
            "search_elapsed_ms": round(elapsed, 2),
            "overrun_ms": round(max(0.0, elapsed - self.limit_ms), 2),
            "completed_candidates": len(self.completed_candidates),
            "checkpoints": self.checkpoints,
            "baseline_completed": self.baseline is not None,
            "baseline_guaranteed": self.baseline is not None,
            "unassigned_probes": "skipped_after_time_limit" if stopped else "full",
            "scope": "cooperative_search; finalization_and_validation_excluded",
            "repeatability_guaranteed": False,
            "cross_run_coverage_guaranteed": False,
        }
