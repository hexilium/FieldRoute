from __future__ import annotations

from datetime import datetime
from statistics import pstdev

from app.domain.models import (
    EngineerRoute,
    EngineerWorkload,
    Job,
    LaborMetrics,
    PlanMetrics,
    PlannedStop,
    PlanResult,
    WorkloadMetrics,
)


def previous_assignment_map(previous: PlanResult | None) -> dict[str, str]:
    if previous is None:
        return {}
    result: dict[str, str] = {}
    for route in previous.routes:
        for stop in route.stops:
            result[stop.job_id] = route.engineer_id
    return result


def previous_stop_map(previous: PlanResult | None) -> dict[str, PlannedStop]:
    if previous is None:
        return {}
    result: dict[str, PlannedStop] = {}
    for route in previous.routes:
        for stop in route.stops:
            result[stop.job_id] = stop
    return result


def build_labor_metrics(
    routes: list[EngineerRoute], *, planning_time: datetime | None = None,
    remaining: bool = False,
) -> LaborMetrics:
    """Count assigned work, routing and waiting at visits, once per engineer.

    Published executing intervals retain their original start and arrival. Clip
    them at planning_time so a replan only counts work still to be performed.
    Availability gaps and unused shift capacity are not time spent on visits.
    Route travel already describes the current plan (including any return leg
    supplied by the routing backend), with executing visits' old travel removed.
    """
    service = waiting = 0.0
    for route in routes:
        for stop in route.stops:
            service_from = max(stop.service_start, planning_time) if planning_time else stop.service_start
            waiting_from = max(stop.arrival, planning_time) if planning_time else stop.arrival
            service += max(0.0, (stop.departure - service_from).total_seconds() / 60)
            waiting += max(0.0, (stop.service_start - waiting_from).total_seconds() / 60)
    service, waiting = round(service, 4), round(waiting, 4)
    travel = round(sum(route.total_travel_minutes for route in routes), 4)
    total = round(service + travel + waiting, 4)
    return LaborMetrics(
        service_minutes=service, travel_minutes=travel, waiting_minutes=waiting,
        total_minutes=total, total_hours=round(total / 60, 4),
        scope="remaining" if remaining else "plan",
    )


def build_workload_metrics(
    routes: list[EngineerRoute], *, planning_time: datetime | None = None,
) -> WorkloadMetrics:
    """Distinguish time spent by people from elapsed time to the final visit.

    Only engineers with assignments participate in distribution aggregates;
    empty routes remain in the detail for transparent staffing comparisons.
    Availability gaps affect completion time, but never assigned person-time.
    """
    engineers = []
    for route in routes:
        labor = build_labor_metrics([route], planning_time=planning_time)
        completion = max(
            (stop.departure for stop in route.stops
             if planning_time is None or stop.departure > planning_time),
            default=None,
        )
        engineers.append(EngineerWorkload(
            engineer_id=route.engineer_id, engineer_name=route.engineer_name,
            assigned_jobs=len(route.stops), service_minutes=labor.service_minutes,
            travel_minutes=labor.travel_minutes, waiting_minutes=labor.waiting_minutes,
            total_minutes=labor.total_minutes, completion_at=completion,
        ))
    active = [engineer.total_minutes for engineer in engineers if engineer.assigned_jobs]
    completion = max(
        (engineer.completion_at for engineer in engineers if engineer.completion_at is not None),
        default=None,
    )
    completion_minutes = None
    if planning_time is not None:
        completion_minutes = (
            round((completion - planning_time).total_seconds() / 60, 4)
            if completion is not None else 0.0
        )
    return WorkloadMetrics(
        planning_time=planning_time, completion_at=completion,
        completion_minutes=completion_minutes, active_engineers=len(active),
        average_engineer_minutes=round(sum(active) / len(active), 4) if active else 0,
        max_engineer_minutes=max(active, default=0),
        engineer_minutes_stddev=round(pstdev(active), 4) if len(active) > 1 else 0,
        engineers=engineers,
    )


def build_metrics(
    routes: list[EngineerRoute],
    total_jobs: int,
    unassigned_count: int,
    changed_assignments: int = 0,
    *,
    jobs: list[Job] | None = None,
    planning_time: datetime | None = None,
    previous_plan: PlanResult | None = None,
    frozen_jobs: int = 0,
    solve_time_ms: float = 0.0,
) -> PlanMetrics:
    assigned = total_jobs - unassigned_count
    distance = sum(r.total_distance_km for r in routes)
    travel = sum(r.total_travel_minutes for r in routes)
    overtime = sum(r.overtime_minutes for r in routes)
    workloads = [r.work_minutes for r in routes]
    imbalance = pstdev(workloads) if len(workloads) > 1 else 0.0

    current_stops = {stop.job_id: stop for route in routes for stop in route.stops}
    previous_stops = previous_stop_map(previous_plan)
    schedule_shift = 0.0
    for job_id, stop in current_stops.items():
        old = previous_stops.get(job_id)
        if old is not None:
            schedule_shift += abs((stop.service_start - old.service_start).total_seconds()) / 60.0

    sla_jobs = 0
    sla_met = 0
    late_minutes = 0.0
    if jobs is not None:
        for job in jobs:
            if job.sla_deadline is None:
                continue
            sla_jobs += 1
            stop = current_stops.get(job.id)
            if stop is not None and stop.service_start <= job.sla_deadline:
                sla_met += 1
            elif stop is not None:
                late_minutes += max(
                    0.0, (stop.service_start - job.sla_deadline).total_seconds() / 60.0
                )

    return PlanMetrics(
        total_jobs=total_jobs,
        assigned_jobs=assigned,
        unassigned_jobs=unassigned_count,
        assignment_rate=(assigned / total_jobs) if total_jobs else 1.0,
        used_engineers=sum(bool(r.stops) for r in routes),
        total_distance_km=round(distance, 2),
        total_travel_minutes=round(travel, 1),
        labor=build_labor_metrics(
            routes, planning_time=planning_time, remaining=previous_plan is not None,
        ),
        # Legacy callers may reconstruct metrics without the request's clock.
        # Without it we cannot establish the same remaining-work scope as the
        # independent validator; leave the optional detail explicitly unknown.
        workload=(build_workload_metrics(routes, planning_time=planning_time)
                  if planning_time is not None else None),
        overtime_minutes=round(overtime, 1),
        workload_stddev=round(imbalance, 1),
        changed_assignments=changed_assignments,
        schedule_shift_minutes=round(schedule_shift, 1),
        frozen_jobs=frozen_jobs,
        sla_jobs=sla_jobs,
        sla_met_jobs=sla_met,
        sla_rate=(sla_met / sla_jobs) if sla_jobs else 1.0,
        late_minutes=round(late_minutes, 1),
        solve_time_ms=round(solve_time_ms, 1),
    )
