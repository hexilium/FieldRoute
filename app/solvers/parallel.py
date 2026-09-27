"""Request-owned spawn workers; deterministic barriers between candidate stages."""

from __future__ import annotations

import asyncio
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from queue import Empty, Full
from time import perf_counter

from app.domain.models import JobStatus
from app.routing.base import RoutingProvider
from app.routing.cached import CachedRoutingProvider
from app.routing.haversine import ASSUMED_SPEEDS_KMH, HaversineRoutingProvider
from app.routing.local_roads import NETWORKS, LocalRoadsRoutingProvider
from app.services.progress import current_progress


def available_cpus() -> int:
    """Respect affinity and Linux container quotas as well as host CPU count."""
    count = (getattr(os, "process_cpu_count", os.cpu_count)() or 1)
    if hasattr(os, "sched_getaffinity"):
        count = min(count, len(os.sched_getaffinity(0)))
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            count = min(count, max(1, int(quota) // int(period)))
    except (OSError, ValueError, ZeroDivisionError):
        try:
            quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text())
            period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text())
            if quota > 0:
                count = min(count, max(1, quota // period))
        except (OSError, ValueError, ZeroDivisionError):
            pass
    return max(1, count)


def delegate(routing):
    return routing.delegate if type(routing) is CachedRoutingProvider else routing


def execution_mode(solver, request) -> dict:
    workers = solver.workers
    reason = "explicit"
    if workers == 0:
        jobs = sum(j.status not in {JobStatus.completed, JobStatus.cancelled, JobStatus.in_progress}
                   for j in request.jobs) if request.engineers else 0
        workers = min(3, available_cpus()) if jobs >= solver.parallel_min_jobs else 1
        reason = "auto" if jobs >= solver.parallel_min_jobs else "small_request"
    if solver.search_strategy == "adaptive":
        workers, reason = 1, "adaptive_attempt_budget"
    elif solver.search_time_limit_ms is not None:
        workers, reason = 1, "time_budget"
    elif type(delegate(solver.routing)) not in {HaversineRoutingProvider, LocalRoadsRoutingProvider}:
        workers, reason = 1, "unsupported_routing"
    return {"mode": "processes" if workers > 1 else "sequential", "workers": workers,
            "requested_workers": solver.workers, "reason": reason,
            "start_method": "spawn" if workers > 1 else None}


class RoadSnapshot(RoutingProvider):
    """Read-only prepared distances; workers never access HTTP or SQLite."""

    def __init__(self, distances, speeds_kmh=None):
        self.distances = distances
        self.speeds_kmh = dict(speeds_kmh or ASSUMED_SPEEDS_KMH)

    def validate_engineer(self, engineer):
        pass

    async def distance_time_for_engineer(self, a, b, engineer):
        key = LocalRoadsRoutingProvider.point(a) + ";" + LocalRoadsRoutingProvider.point(b)
        # Missing preparation is an error, never a fallback to straight lines.
        km = self.distances[NETWORKS[engineer.travel_mode]][key]
        speed = engineer.travel_speed_kmh or self.speeds_kmh[engineer.travel_mode]
        return km, km / speed * 60

    async def distance_time(self, a, b):
        raise RuntimeError("The search must use the engineer's travel profile")


def routing_snapshot(routing):
    source = delegate(routing)
    if type(source) is HaversineRoutingProvider:
        snapshot = HaversineRoutingProvider(source.average_speed_kmh, source.road_factor,
                                           speeds_kmh=source.speeds_kmh)
    elif type(source) is LocalRoadsRoutingProvider:
        snapshot = RoadSnapshot(source.distances, source.speeds_kmh)
    else:
        raise ValueError("Routing provider cannot be used in worker processes")
    if type(routing) is CachedRoutingProvider:
        snapshot = CachedRoutingProvider(snapshot, precision=routing.precision,
                                         point_cache_size=routing.point_cache_size)
    return snapshot


class WorkerProgress:
    def __init__(self, queue, cancelled):
        self.queue, self.cancelled = queue, cancelled
        self.state = {}
        self.last_sent = 0.0

    def update(self, *, force=False, **fields):
        if self.cancelled.is_set():
            raise RuntimeError("Planning worker cancelled")
        self.state.update(fields)
        now = perf_counter()
        if force or now - self.last_sent >= 0.2:
            self.last_sent = now
            try:
                self.queue.put_nowait((os.getpid(), dict(self.state)))
            except Full:
                pass  # Progress is best-effort; results travel on the executor channel.

    def committed(self, routes):
        self.update(best_assigned=max(self.state.get("best_assigned", 0),
                                      sum(len(r.visits) for r in routes.values())))


_searches = None
_urgent_searches = None
_loop = None


def initialize_worker(request, routing, queue, cancelled):
    from app.domain.models import OptimizationPolicy
    from app.solvers.insertion import Search
    from app.solvers.scheduling import ScheduleEvaluator

    global _searches, _urgent_searches, _loop
    # Parent drains the bounded queue, but a disappearing parent must not leave
    # a process waiting for its progress feeder thread at interpreter shutdown.
    queue.cancel_join_thread()
    current_progress.set(WorkerProgress(queue, cancelled))
    evaluator = ScheduleEvaluator(request, routing)
    # The original candidate stages retain the v10 objective and seeds. Urgent,
    # Or-opt, CROSS-exchange, deep/cyclic repair, cyclic reinsertion and residual
    # stability repair use the configured urgency ordering.
    _searches = {
        p: Search(request.model_copy(update={"optimization_policy": p}), routing, evaluator,
                  legacy_objective=True)
        for p in OptimizationPolicy
    }
    _urgent_searches = {
        p: Search(request.model_copy(update={"optimization_policy": p}), routing, evaluator)
        for p in OptimizationPolicy
    }
    _loop = asyncio.new_event_loop()


def run_candidate(policy, stage, seed):
    search = (
        _urgent_searches
        if stage in {
            "urgent", "or_opt", "cross_exchange", "deep_repair", "stability_repair",
            "cyclic_exchange", "cyclic_reinsertion", "ruin_recreate",
        }
        else _searches
    )[policy]
    before = search.moves
    progress = current_progress.get()
    progress.update(force=True, stage=policy.value + ":" + stage, processed_jobs=0, pass_name="")
    routes, diagnostics = _loop.run_until_complete(search.candidate(stage, seed))
    progress.update(force=True, schedule_attempts=search.evaluator.attempts)
    return routes, diagnostics, search.moves - before, os.getpid(), {
        "schedule_attempts": search.evaluator.attempts,
        "route_evaluations": search.evaluator.evaluations,
        "schedule_cache": search.evaluator.stats,
    }


class ParallelCandidates:
    def __init__(self, request, routing, workers):
        self.request, self.routing, self.workers = request, routing, workers
        self.progress = current_progress.get()
        self.states = {}
        self.statistics = {}
        self.executor = None

    async def __aenter__(self):
        context = multiprocessing.get_context("spawn")
        self.queue = context.Queue(maxsize=32)
        self.cancelled = context.Event()
        self.executor = ProcessPoolExecutor(
            max_workers=self.workers, mp_context=context, initializer=initialize_worker,
            initargs=(self.request, routing_snapshot(self.routing), self.queue, self.cancelled),
        )
        return self

    async def __aexit__(self, *exc):
        self.cancelled.set()
        # Joining in a thread keeps the API responsive during cancellation/cleanup.
        try:
            await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
        finally:
            self.queue.close()
            self.queue.join_thread()

    def drain(self):
        while True:
            try:
                pid, state = self.queue.get_nowait()
            except Empty:
                break
            self.states[pid] = state
        if self.progress is not None and self.states:
            self.progress.update(
                schedule_attempts=sum(s.get("schedule_attempts", 0) for s in self.states.values()),
                best_assigned=max(self.progress.state.get("best_assigned", 0),
                                  max(s.get("best_assigned", 0) for s in self.states.values())),
                worker_stages=[s.get("stage", "") for s in self.states.values()],
            )

    async def run(self, items, *, complete=True):
        if self.progress is not None:
            self.progress.update(force=True, phase="search", stage="parallel:" + items[0][1],
                                 workers=self.workers, processed_jobs=0, pass_name="")
        loop = asyncio.get_running_loop()
        futures = [loop.run_in_executor(self.executor, run_candidate, *item) for item in items]
        pending = set(futures)
        try:
            while pending:
                done, pending = await asyncio.wait(pending, timeout=0.1,
                                                   return_when=asyncio.FIRST_COMPLETED)
                self.drain()
                for future in done:
                    routes, _, _, pid, stats = future.result()  # Fail promptly on worker errors.
                    self.statistics[pid] = stats
                    if self.progress is not None:
                        self.progress.committed(routes)
                        if complete:
                            self.progress.update(force=True, completed_candidates=
                                                 self.progress.state["completed_candidates"] + 1)
            return [future.result()[:3] for future in futures]
        finally:
            # Retrieve exceptions from every future, including after cancellation.
            for future in futures:
                if not future.done():
                    future.cancel()
                elif not future.cancelled():
                    future.exception()

    def report(self):
        # Stable ordering by first completed task is not a search input.
        return list(self.statistics.values())
