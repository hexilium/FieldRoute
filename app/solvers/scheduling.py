"""Feasibility and schedule evaluation shared by the two new local solvers."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import isfinite
from typing import TYPE_CHECKING, Any

from app.domain.models import DistrictMode, Engineer, Job, JobStatus, PlanRequest, PlanResult
from app.domain.priorities import is_emergency
from app.routing.base import RoutingProvider
from app.services.metrics import previous_assignment_map, previous_stop_map
from app.services.progress import current_progress
from app.services.stability import forced_assignment
from app.solvers.deadline import SearchDeadline

if TYPE_CHECKING:
    from app.solvers.adaptive import AttemptController


@dataclass(frozen=True)
class Visit:
    job: Job
    arrival: datetime
    start: datetime
    end: datetime
    km: float
    minutes: float
    executing: bool = False


@dataclass(frozen=True)
class Schedule:
    jobs: tuple[str, ...]
    visits: tuple[Visit, ...]
    distance: float = 0.0
    travel: float = 0.0
    soft_cost: float = 0.0
    sla_late_jobs: int = 0
    late_minutes: float = 0.0
    urgent_start_minutes: float = 0.0
    assigned_job_ids: frozenset[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Includes executing visits, which are absent from the future job sequence.
        object.__setattr__(self, "assigned_job_ids", frozenset(v.job.id for v in self.visits))


@dataclass(frozen=True)
class ScheduleFailure:
    code: str
    job_id: str | None = None
    facts: dict[str, Any] | None = None


class ScheduleEvaluator:
    def __init__(
        self, request: PlanRequest, routing: RoutingProvider, *, prefix_cache_size: int = 4096,
        insertion_failure_cache_size: int = 65536,
    ) -> None:
        if prefix_cache_size < 0 or insertion_failure_cache_size < 0:
            raise ValueError("cache sizes must be nonnegative")
        self.request = request
        self.search_deadline: SearchDeadline | None = None
        self.attempt_control: AttemptController | None = None
        self.routing = routing
        self.engineers = {e.id: e for e in request.engineers}
        for engineer in self.engineers.values():
            routing.validate_engineer(engineer)
        self.jobs = {
            j.id: j
            for j in request.jobs
            if j.status not in {JobStatus.completed, JobStatus.cancelled}
        }
        self.previous = previous_assignment_map(request.previous_plan)
        self.old_stops = previous_stop_map(request.previous_plan)
        self.forced = {
            j.id: forced_assignment(j, request, self.previous, self.old_stops)[0]
            for j in self.jobs.values()
        }
        self.executing: dict[str, Visit] = {}
        for job in self.jobs.values():
            if job.status == JobStatus.in_progress:
                old = self.old_stops[job.id]
                engineer_id = self.previous[job.id]
                self.executing[engineer_id] = Visit(
                    job, old.arrival, old.service_start, old.departure, 0.0, 0.0, True
                )
        self.movable = [j for j in self.jobs.values() if j.status != JobStatus.in_progress]
        # Cache on first use, after eligibility and travel checks. In particular,
        # an unused or executing job's duration must not introduce a new overflow.
        self._job_timing: dict[str, tuple[timedelta, timedelta]] = {}
        self._eligibility_cache: OrderedDict[tuple[str, str], tuple[str, ...]] = OrderedDict()
        self.cache: OrderedDict[
            tuple[str, tuple[str, ...]], tuple[Schedule | None, ScheduleFailure | None]
        ] = OrderedDict()
        self.evaluations = 0
        self.attempts = 0
        self.progress = current_progress.get()
        # A failed position is reusable while the complete base route is unchanged.
        # Bit masks retain many failures cheaply, without storing visits/facts for
        # millions of rejected schedules. Scope includes engineer and request.
        self.insertion_failures: OrderedDict[tuple[str, tuple[str, ...], str], int] = OrderedDict()
        self.insertion_failure_cache_size = insertion_failure_cache_size
        self.insertion_failure_hits = 0
        self.insertion_bounds: OrderedDict[tuple[str, tuple[str, ...]], tuple[datetime, ...]] = OrderedDict()
        self.insertion_bound_checks = 0
        self.insertion_bound_rejections = 0
        # Only the beginning of a route is reusable: changing an earlier visit
        # changes arrival times throughout its suffix. Both caches belong to this
        # fixed request, including engineer profiles and executing intervals.
        self.prefix_cache_size = prefix_cache_size
        self.prefix_cache: OrderedDict[
            tuple[str, tuple[str, ...]], tuple[Visit, ...] | ScheduleFailure
        ] = OrderedDict()
        self.prefix_hits = 0
        self.prefix_failure_hits = 0
        self.prefix_visits_reused = 0

    @property
    def stats(self) -> dict[str, int]:
        return {
            "route_entries": len(self.cache),
            "prefix_entries": len(self.prefix_cache),
            "max_prefix_entries": self.prefix_cache_size,
            "prefix_hits": self.prefix_hits,
            "prefix_failure_hits": self.prefix_failure_hits,
            "prefix_visits_reused": self.prefix_visits_reused,
            "eligibility_entries": len(self._eligibility_cache),
            "max_eligibility_entries": 4096,
            "insertion_failure_entries": len(self.insertion_failures),
            "max_insertion_failure_entries": self.insertion_failure_cache_size,
            "insertion_failure_hits": self.insertion_failure_hits,
            "insertion_bound_entries": len(self.insertion_bounds),
            "max_insertion_bound_entries": 4096,
            "insertion_bound_checks": self.insertion_bound_checks,
            "insertion_bound_rejections": self.insertion_bound_rejections,
        }

    def remember_prefix(
        self, engineer_id: str, job_ids: tuple[str, ...],
        value: tuple[Visit, ...] | ScheduleFailure,
    ) -> None:
        if not self.prefix_cache_size:
            return
        key = engineer_id, job_ids
        self.prefix_cache[key] = value
        if len(self.prefix_cache) > self.prefix_cache_size:
            self.prefix_cache.popitem(last=False)

    def eligibility(self, job: Job, engineer: Engineer) -> list[str]:
        reasons = []
        if self.request.district_mode == DistrictMode.strict and (
            job.district_id is None or engineer.district_id is None
            or job.district_id != engineer.district_id
        ):
            reasons.append("district_mismatch")
        if not engineer.available:
            reasons.append("engineer_unavailable")
        if not job.required_skills <= engineer.skills:
            reasons.append("skills")
        if not job.required_equipment <= engineer.equipment:
            reasons.append("equipment")
        if job.required_transport and job.required_transport not in engineer.transport_modes:
            reasons.append("transport")
        owner = self.forced[job.id]
        if owner and owner != engineer.id:
            reasons.append("locked_to_other_engineer")
        return reasons

    async def evaluate(
        self, engineer_id: str, job_ids: tuple[str, ...]
    ) -> tuple[Schedule | None, str | None]:
        schedule, failure = await self.diagnose(engineer_id, job_ids)
        return schedule, failure.code if failure else None

    async def evaluate_with_preferred_starts(
        self,
        engineer_id: str,
        job_ids: tuple[str, ...],
        preferred_starts: dict[str, datetime],
    ) -> tuple[Schedule | None, ScheduleFailure | None]:
        """Evaluate a route while retaining published starts where slack permits.

        The cached route remains the earliest feasible schedule. Retiming is a
        separate immutable value, so preferred starts neither reuse a published
        wait for another call nor poison the ordinary insertion caches.
        """
        schedule, failure = await self.diagnose(engineer_id, job_ids)
        if schedule is None:
            return None, failure
        return self.retime(engineer_id, schedule, preferred_starts), None

    def retime(
        self,
        engineer_id: str,
        schedule: Schedule,
        preferred_starts: dict[str, datetime],
    ) -> Schedule:
        """Delay an earliest schedule toward preferences without losing feasibility."""
        current = self.executing.get(engineer_id)
        fixed = int(current is not None)
        future = schedule.visits[fixed:]
        if not future or not preferred_starts:
            return schedule

        engineer = self.engineers[engineer_id]
        if engineer.shift.end.tzinfo.utcoffset(None) is None or any(
            window.start.tzinfo.utcoffset(None) is None
            or window.end.tzinfo.utcoffset(None) is None
            for visit in future
            for window in visit.job.time_windows
        ):
            return schedule

        # Latest feasible starts reserve enough time for the complete suffix.
        # Choosing any earlier point in a visit's interval therefore cannot turn
        # an earliest-feasible fixed sequence into an infeasible one.
        latest_reversed = []
        end = engineer.shift.end
        try:
            for index in range(len(future) - 1, -1, -1):
                visit = future[index]
                duration, margin = self.job_timing(visit.job)
                latest = end - duration
                if visit.job.time_windows:
                    choices = [
                        min(latest, window.end - margin)
                        for window in visit.job.time_windows
                        if window.start <= min(latest, window.end - margin)
                    ]
                    if not choices:
                        return schedule
                    latest = max(choices)
                latest_reversed.append(latest)
                if index:
                    end = latest - timedelta(minutes=visit.minutes)
        except (OverflowError, ValueError):
            return schedule
        latest_starts = tuple(reversed(latest_reversed))

        visits = list(schedule.visits[:fixed])
        for index, visit in enumerate(future):
            arrival = (
                visit.arrival
                if index == 0
                else visits[-1].end + timedelta(minutes=visit.minutes)
            )
            duration, margin = self.job_timing(visit.job)
            intervals = (
                [
                    (
                        max(arrival, window.start),
                        min(latest_starts[index], window.end - margin),
                    )
                    for window in visit.job.time_windows
                ]
                if visit.job.time_windows
                else [(arrival, latest_starts[index])]
            )
            feasible = [(lower, upper) for lower, upper in intervals if lower <= upper]
            if not feasible:
                return schedule
            preferred = preferred_starts.get(visit.job.id)
            if preferred is None:
                start = min(lower for lower, _ in feasible)
            else:
                choices = [min(max(preferred, lower), upper) for lower, upper in feasible]
                start = min(
                    choices,
                    key=lambda value: (abs((value - preferred).total_seconds()), value),
                )
            visits.append(
                Visit(
                    visit.job,
                    arrival,
                    start,
                    start + duration,
                    visit.km,
                    visit.minutes,
                )
            )
        return self.summarize(engineer_id, schedule.jobs, tuple(visits))

    async def evaluate_insertion(
        self, engineer_id: str, route: Schedule, job_id: str, position: int,
    ) -> Schedule | None:
        """Reuse a feasible route's unchanged prefix even after cache eviction.

        The route must come from this evaluator for this engineer (earliest
        feasible times, rather than externally supplied published waiting).
        Only the inserted visit and following visits need to be rescheduled.
        """
        await self.check_attempt()
        key = engineer_id, route.jobs, job_id
        bit = 1 << position
        failed = self.insertion_failures.get(key, 0)
        if failed & bit:
            self.insertion_failure_hits += 1
            return None
        if not await self.insertion_fits(engineer_id, route, job_id, position):
            self.insertion_bound_rejections += 1
            self.remember_insertion_failure(key, failed | bit)
            return None
        ids = route.jobs[:position] + (job_id,) + route.jobs[position:]
        prefix = route.visits[:position + bool(self.executing.get(engineer_id))]
        schedule, _ = await self.diagnose(engineer_id, ids, prefix=prefix, count_attempt=False)
        if schedule is None and self.insertion_failure_cache_size:
            self.remember_insertion_failure(key, failed | bit)
        return schedule

    def remember_insertion_failure(
        self, key: tuple[str, tuple[str, ...], str], positions: int,
    ) -> None:
        if not self.insertion_failure_cache_size:
            return
        if (key not in self.insertion_failures
                and len(self.insertion_failures) >= self.insertion_failure_cache_size):
            self.insertion_failures.popitem(last=False)
        self.insertion_failures[key] = positions

    def latest_starts(self, engineer_id: str, route: Schedule) -> tuple[datetime, ...] | None:
        """Latest feasible service starts of the unchanged suffix, in reverse.

        Use actual directed legs and rounded timedeltas, without triangle-inequality
        assumptions. Variable-offset time zones fall back to full scheduling:
        their wall-clock timedelta arithmetic need not be translation-invariant.
        """
        key = engineer_id, route.jobs
        cached = self.insertion_bounds.get(key)
        if cached is not None:
            return cached
        visits = route.visits[bool(self.executing.get(engineer_id)):]
        end = self.engineers[engineer_id].shift.end
        if end.tzinfo.utcoffset(None) is None:
            return None
        result = []
        for index in range(len(visits) - 1, -1, -1):
            visit = visits[index]
            job = visit.job
            if any(w.start.tzinfo.utcoffset(None) is None or w.end.tzinfo.utcoffset(None) is None
                   for w in job.time_windows):
                return None
            duration, margin = self.job_timing(job)
            try:
                latest = end - duration
                if job.time_windows:
                    latest = max(min(latest, w.end - margin) for w in job.time_windows
                                 if w.start <= min(latest, w.end - margin))
                # The leg into the first future visit is replaced by the insertion.
                if index:
                    end = latest - timedelta(minutes=visit.minutes)
            except (OverflowError, ValueError):
                # A redundant window near datetime.min can underflow backwards
                # even though forward scheduling succeeds through another window.
                return None
            result.append(latest)
        value = tuple(reversed(result))
        if len(self.insertion_bounds) >= 4096:
            self.insertion_bounds.popitem(last=False)
        self.insertion_bounds[key] = value
        return value

    async def insertion_fits(
        self, engineer_id: str, route: Schedule, job_id: str, position: int,
    ) -> bool:
        """Necessary feasibility check before allocating/rescheduling a candidate.

        Survivors still pass the ordinary evaluator; rejections are never used
        as diagnostic failure facts. Every attempted position keeps its budget.
        """
        self.insertion_bound_checks += 1
        engineer, job = self.engineers[engineer_id], self.jobs[job_id]
        current = self.executing.get(engineer_id)
        if engineer.max_jobs is not None and len(route.jobs) + 1 + bool(current) > engineer.max_jobs:
            return False
        if self.eligibility(job, engineer):
            return False
        offset = position + bool(current)
        if position:
            moment, origin = route.visits[offset - 1].end, route.visits[offset - 1].job.location
        else:
            moment = max(self.request.planning_time, engineer.shift.start)
            if engineer.available_from:
                moment = max(moment, engineer.available_from)
            origin = engineer.current_location or engineer.start_location
            if current:
                moment, origin = max(moment, current.end), current.job.location
        if moment.tzinfo.utcoffset(None) is None:
            return True
        km, minutes = await self.routing.distance_time_for_engineer(origin, job.location, engineer)
        if self.search_deadline is not None:
            self.search_deadline.check()
        if not all(isfinite(x) and x >= 0 for x in (km, minutes)):
            return False
        arrival = moment + timedelta(minutes=minutes)
        duration, margin = self.job_timing(job)
        starts = [max(arrival, w.start) for w in job.time_windows
                  if max(arrival, w.start) + margin <= w.end]
        if job.time_windows and not starts:
            return False
        start = min(starts) if starts else arrival
        end = start + duration
        if end > engineer.shift.end:
            return False
        if position == len(route.jobs) or end.tzinfo.utcoffset(None) is None:
            return True
        bounds = self.latest_starts(engineer_id, route)
        if bounds is None:
            return True
        km, minutes = await self.routing.distance_time_for_engineer(
            job.location, route.visits[offset].job.location, engineer,
        )
        if self.search_deadline is not None:
            self.search_deadline.check()
        return (all(isfinite(x) and x >= 0 for x in (km, minutes))
                and end + timedelta(minutes=minutes) <= bounds[position])

    def job_timing(self, job: Job) -> tuple[timedelta, timedelta]:
        timing = self._job_timing.get(job.id)
        if timing is None:
            duration = timedelta(minutes=job.service_minutes)
            timing = duration, duration if job.window_semantics == "completion" else timedelta(0)
            self._job_timing[job.id] = timing
        return timing

    async def check_attempt(self) -> None:
        if self.attempt_control is not None:
            self.attempt_control.take()
        self.attempts += 1
        if self.attempts % 128 == 0:
            if self.progress is not None:
                self.progress.update(schedule_attempts=self.attempts)
            # Cached/local routing awaits often complete synchronously. Explicitly
            # yield so HTTP progress, health requests and cancellation can run.
            await asyncio.sleep(0)
        if self.search_deadline is not None:
            self.search_deadline.check()

    async def diagnose(
        self, engineer_id: str, job_ids: tuple[str, ...], *, prefix: tuple[Visit, ...] | None = None,
        count_attempt: bool = True,
    ) -> tuple[Schedule | None, ScheduleFailure | None]:
        if count_attempt:
            await self.check_attempt()
        key = engineer_id, job_ids
        if key not in self.cache:
            value = await self._evaluate(engineer_id, job_ids, prefix=prefix)
            if len(self.cache) >= 4096:
                self.cache.popitem(last=False)
            self.cache[key] = value
        if self.search_deadline is not None:
            self.search_deadline.check()
        return self.cache[key]

    async def _evaluate(
        self, engineer_id: str, job_ids: tuple[str, ...], *, prefix: tuple[Visit, ...] | None = None,
    ) -> tuple[Schedule | None, ScheduleFailure | None]:
        self.evaluations += 1
        engineer = self.engineers[engineer_id]
        current = self.executing.get(engineer_id)
        if engineer.max_jobs is not None and len(job_ids) + bool(current) > engineer.max_jobs:
            return None, ScheduleFailure("max_jobs", facts={
                "jobs": len(job_ids) + bool(current), "limit": engineer.max_jobs,
            })
        moment = max(self.request.planning_time, engineer.shift.start)
        if engineer.available_from:
            moment = max(moment, engineer.available_from)
        origin = engineer.current_location or engineer.start_location
        visits = []
        if current:
            visits.append(current)
            moment = max(moment, current.end)
            origin = current.job.location
        offset = 0
        if prefix is not None:
            visits = list(prefix)
            offset = len(prefix) - bool(current)
            if offset:
                moment, origin = prefix[-1].end, prefix[-1].job.location
        elif self.prefix_cache_size:
            for length in range(len(job_ids), 0, -1):
                prefix = self.prefix_cache.get((engineer_id, job_ids[:length]))
                if prefix is None:
                    continue
                self.prefix_hits += 1
                if isinstance(prefix, ScheduleFailure):
                    self.prefix_failure_hits += 1
                    return None, prefix
                visits = list(prefix)
                moment, origin = visits[-1].end, visits[-1].job.location
                offset = length
                self.prefix_visits_reused += length
                break
        for index in range(offset, len(job_ids)):
            if self.search_deadline is not None:
                self.search_deadline.check()
            job_id = job_ids[index]
            job = self.jobs[job_id]
            eligibility_key = engineer_id, job_id
            reasons = self._eligibility_cache.get(eligibility_key)
            if reasons is None:
                reasons = tuple(self.eligibility(job, engineer))
                if len(self._eligibility_cache) >= 4096:
                    self._eligibility_cache.popitem(last=False)
                self._eligibility_cache[eligibility_key] = reasons
            if reasons:
                failure = ScheduleFailure(reasons[0], job_id, {"reasons": list(reasons)})
                self.remember_prefix(engineer_id, job_ids[:index + 1], failure)
                return None, failure
            km, minutes = await self.routing.distance_time_for_engineer(origin, job.location, engineer)
            if self.search_deadline is not None:
                self.search_deadline.check()
            if not all(isfinite(x) and x >= 0 for x in (km, minutes)):
                failure = ScheduleFailure("unreachable", job_id)
                self.remember_prefix(engineer_id, job_ids[:index + 1], failure)
                return None, failure
            arrival = moment + timedelta(minutes=minutes)
            duration, window_margin = self.job_timing(job)
            starts = [
                max(arrival, w.start)
                for w in job.time_windows
                if max(arrival, w.start) + window_margin <= w.end
            ]
            if job.time_windows and not starts:
                nearest = min(job.time_windows, key=lambda w: max(arrival, w.start) + window_margin - w.end)
                constraint_time = max(arrival, nearest.start) + window_margin
                failure = ScheduleFailure("time_window", job_id, {
                    "arrival": arrival, "constraint_time": constraint_time,
                    "window_start": nearest.start, "window_end": nearest.end,
                    "window_semantics": job.window_semantics,
                    "service_minutes": job.service_minutes,
                    "overrun_minutes": (constraint_time - nearest.end).total_seconds() / 60,
                })
                self.remember_prefix(engineer_id, job_ids[:index + 1], failure)
                return None, failure
            # In completion mode, the entire service must fit within one window.
            start = min(starts) if starts else arrival
            end = start + duration
            if end > engineer.shift.end:
                failure = ScheduleFailure("shift_end", job_id, {
                    "departure": end, "shift_end": engineer.shift.end,
                    "overrun_minutes": (end - engineer.shift.end).total_seconds() / 60,
                })
                self.remember_prefix(engineer_id, job_ids[:index + 1], failure)
                return None, failure
            visits.append(Visit(job, arrival, start, end, km, minutes))
            self.remember_prefix(engineer_id, job_ids[:index + 1], tuple(visits))
            moment, origin = end, job.location
        return self.summarize(engineer_id, job_ids, tuple(visits)), None

    async def published_routes(self, plan: PlanResult) -> dict[str, Schedule]:
        """Restore a previously validated plan, preserving published extra waiting.

        Callers validate the source snapshot first. These schedules are suitable
        for scoring and display; insertion fast paths require earliest schedules
        instead, so edited sequences must pass through full diagnose/evaluate.
        """
        published = {route.engineer_id: route for route in plan.routes}
        routes = {}
        for eid, engineer in self.engineers.items():
            origin = engineer.current_location or engineer.start_location
            visits, ids = [], []
            for stop in published[eid].stops if eid in published else []:
                job = self.jobs[stop.job_id]
                executing = job.status == JobStatus.in_progress
                km, minutes = (
                    (0.0, 0.0) if executing else
                    await self.routing.distance_time_for_engineer(origin, job.location, engineer)
                )
                visits.append(Visit(
                    job, stop.arrival, stop.service_start, stop.departure, km, minutes, executing,
                ))
                if not executing:
                    ids.append(job.id)
                origin = job.location
            routes[eid] = self.summarize(eid, tuple(ids), tuple(visits))
        return routes

    def summarize(
        self, engineer_id: str, job_ids: tuple[str, ...], visits: tuple[Visit, ...]
    ) -> Schedule:
        """Cost of a schedule, including validated published times with extra waiting."""
        distance = travel = soft = 0.0
        sla_late_jobs = 0
        late_minutes = 0.0
        urgent_start_minutes = 0.0
        weights = self.request.weights
        for visit in visits:
            job, start = visit.job, visit.start
            if job.sla_deadline and start > job.sla_deadline:
                late = (start - job.sla_deadline).total_seconds() / 60
                sla_late_jobs += 1
                late_minutes += late
                if not visit.executing:
                    soft += weights.sla_violation + late
            if visit.executing:
                continue
            if is_emergency(job, self.request):
                urgent_start_minutes += max(
                    0.0, (start - self.request.planning_time).total_seconds() / 60,
                )
            if job.id in self.previous and self.previous[job.id] != engineer_id:
                soft += weights.plan_churn
            if job.id in self.old_stops:
                soft += (
                    weights.schedule_shift
                    * abs((start - self.old_stops[job.id].service_start).total_seconds())
                    / 60
                )
            soft += weights.travel_minutes * visit.minutes
            distance += visit.km
            travel += visit.minutes
        return Schedule(
            job_ids, tuple(visits), distance, travel, soft, sla_late_jobs, late_minutes,
            urgent_start_minutes,
        )
