"""Independent result checks. Does not use solver schedule/eligibility helpers."""

from __future__ import annotations

from datetime import timedelta
from math import isclose, isfinite
from statistics import pstdev

from app.domain.models import (
    DistrictMode,
    EmergencyReplanPolicy,
    JobStatus,
    PlanRequest,
    PlanResult,
)
from app.domain.priorities import has_route_changing_emergency
from app.routing.base import RoutingProvider


async def validate_plan(
    request: PlanRequest, result: PlanResult, routing: RoutingProvider
) -> list[str]:
    errors: list[str] = []
    engineers = {e.id: e for e in request.engineers}
    for engineer in engineers.values():
        routing.validate_engineer(engineer)
    if request.district_mode == DistrictMode.strict:
        for engineer in request.engineers:
            if engineer.district_id is None:
                errors.append(f"missing_engineer_district:{engineer.id}")
        for job in request.jobs:
            if job.status not in {JobStatus.completed, JobStatus.cancelled} and job.district_id is None:
                errors.append(f"missing_job_district:{job.id}")
    active = {
        j.id: j for j in request.jobs if j.status not in {JobStatus.completed, JobStatus.cancelled}
    }
    old = {
        s.job_id: (r.engineer_id, s)
        for r in (request.previous_plan.routes if request.previous_plan else [])
        for s in r.stops
    }
    assigned: set[str] = set()
    seen_engineers: set[str] = set()
    freeze_until = request.planning_time + timedelta(minutes=request.freeze_horizon_minutes)
    emergency_override = (
        request.emergency_replan_policy == EmergencyReplanPolicy.reroute_future
        and has_route_changing_emergency(request)
    )
    for route in result.routes:
        eid = route.engineer_id
        if eid not in engineers or eid in seen_engineers:
            errors.append(f"invalid_or_duplicate_engineer:{eid}")
            continue
        seen_engineers.add(eid)
        engineer = engineers[eid]
        if engineer.max_jobs is not None and len(route.stops) > engineer.max_jobs:
            errors.append(f"max_jobs:{eid}")
        moment = max(request.planning_time, engineer.shift.start)
        if engineer.available_from:
            moment = max(moment, engineer.available_from)
        origin = engineer.current_location or engineer.start_location
        total_km = total_minutes = 0.0
        for index, stop in enumerate(route.stops):
            jid = stop.job_id
            if jid not in active or jid in assigned:
                errors.append(f"invalid_or_duplicate_job:{jid}")
                continue
            assigned.add(jid)
            job = active[jid]
            # Independent of solver eligibility, including already executing work.
            if request.district_mode == DistrictMode.strict and (
                job.district_id is None or engineer.district_id is None
                or job.district_id != engineer.district_id
            ):
                errors.append(f"district_mismatch:{jid}")
            if (stop.location.lat, stop.location.lon) != (job.location.lat, job.location.lon):
                errors.append(f"location:{jid}")
            if job.status == JobStatus.in_progress:
                previous = old.get(jid)
                if previous is None or previous[0] != eid or index != 0:
                    errors.append(f"execution_owner:{jid}")
                elif (stop.arrival, stop.service_start, stop.departure) != (
                    previous[1].arrival,
                    previous[1].service_start,
                    previous[1].departure,
                ):
                    errors.append(f"execution_interval:{jid}")
                if stop.travel_minutes_from_previous or stop.distance_km_from_previous:
                    errors.append(f"execution_residual_travel:{jid}")
                moment = max(moment, stop.departure)
                origin = job.location
                continue
            if not engineer.available:
                errors.append(f"unavailable:{jid}")
            if not job.required_skills <= engineer.skills:
                errors.append(f"skills:{jid}")
            if not job.required_equipment <= engineer.equipment:
                errors.append(f"equipment:{jid}")
            if job.required_transport and job.required_transport not in engineer.transport_modes:
                errors.append(f"transport:{jid}")
            required_owner = None
            if job.assigned_engineer_id and (job.locked or job.status == JobStatus.assigned):
                required_owner = job.assigned_engineer_id
            elif jid in old:
                old_eid, old_stop = old[jid]
                if job.locked or (
                    request.freeze_horizon_minutes > 0
                    and not emergency_override
                    and old_eid in engineers
                    and engineers[old_eid].available
                    and request.planning_time <= old_stop.service_start <= freeze_until
                ):
                    required_owner = old_eid
            if required_owner and eid != required_owner:
                errors.append(f"lock:{jid}")
            km, minutes = await routing.distance_time_for_engineer(origin, job.location, engineer)
            if not all(isfinite(x) and x >= 0 for x in (km, minutes)):
                errors.append(f"unreachable:{jid}")
                continue
            expected_arrival = moment + timedelta(minutes=minutes)
            if abs((stop.arrival - expected_arrival).total_seconds()) > (
                routing.arrival_tolerance_seconds(engineer)
            ):
                errors.append(f"arrival:{jid}")
            if stop.service_start < stop.arrival:
                errors.append(f"start_before_arrival:{jid}")
            if job.time_windows and not any(
                w.start <= stop.service_start <= w.end
                and (job.window_semantics == "start" or stop.departure <= w.end)
                for w in job.time_windows
            ):
                errors.append(f"time_window:{jid}")
            if (
                abs(
                    (stop.departure - stop.service_start).total_seconds() - job.service_minutes * 60
                )
                > 0.01
            ):
                errors.append(f"duration:{jid}")
            if stop.departure > engineer.shift.end:
                errors.append(f"shift_end:{jid}")
            if not isclose(stop.distance_km_from_previous, km, abs_tol=0.011):
                errors.append(f"distance:{jid}")
            if not isclose(stop.travel_minutes_from_previous, minutes, abs_tol=0.051):
                errors.append(f"travel:{jid}")
            total_km += km
            total_minutes += minutes
            origin, moment = job.location, stop.departure
        if not isclose(route.total_distance_km, total_km, abs_tol=0.011):
            errors.append(f"route_distance:{eid}")
        if not isclose(route.total_travel_minutes, total_minutes, abs_tol=0.051):
            errors.append(f"route_travel:{eid}")
    unassigned = [u.job_id for u in result.unassigned]
    if len(unassigned) != len(set(unassigned)) or set(unassigned) & assigned:
        errors.append("duplicate_unassigned")
    if assigned | set(unassigned) != set(active):
        errors.append("job_accounting")
    if any(j.status == JobStatus.in_progress and j.id not in assigned for j in active.values()):
        errors.append("lost_execution")
    m = result.metrics
    if (m.total_jobs, m.assigned_jobs, m.unassigned_jobs, m.used_engineers) != (
        len(active),
        len(assigned),
        len(unassigned),
        sum(bool(r.stops) for r in result.routes),
    ):
        errors.append("metrics_counts")
    if not isclose(
        m.total_distance_km, sum(r.total_distance_km for r in result.routes), abs_tol=0.011
    ):
        errors.append("metrics_distance")
    if not isclose(
        m.total_travel_minutes, sum(r.total_travel_minutes for r in result.routes), abs_tol=0.051
    ):
        errors.append("metrics_travel")
    if m.labor is not None:
        # Independently recompute from published intervals. Old snapshots without
        # this optional metric are accepted and enriched by state restoration.
        service_seconds = waiting_seconds = 0.0
        for route in result.routes:
            for stop in route.stops:
                service_seconds += max(
                    0.0, (stop.departure - max(stop.service_start, request.planning_time)).total_seconds(),
                )
                waiting_seconds += max(
                    0.0, (stop.service_start - max(stop.arrival, request.planning_time)).total_seconds(),
                )
        expected_service = round(service_seconds / 60, 4)
        expected_waiting = round(waiting_seconds / 60, 4)
        expected_travel = round(sum(r.total_travel_minutes for r in result.routes), 4)
        expected_total = round(expected_service + expected_waiting + expected_travel, 4)
        expected_labor = {
            "service_minutes": expected_service, "travel_minutes": expected_travel,
            "waiting_minutes": expected_waiting, "total_minutes": expected_total,
            "total_hours": round(expected_total / 60, 4),
        }
        for field, expected in expected_labor.items():
            if not isclose(getattr(m.labor, field), expected, rel_tol=0, abs_tol=0.00011):
                errors.append("metrics_labor_" + field)
        if m.labor.scope != ("remaining" if request.previous_plan is not None else "plan"):
            errors.append("metrics_labor_scope")
    if m.workload is not None:
        workload = m.workload
        if workload.planning_time != request.planning_time:
            errors.append("metrics_workload_planning_time")
        rows = {row.engineer_id: row for row in workload.engineers}
        route_ids = {route.engineer_id for route in result.routes}
        if len(rows) != len(workload.engineers) or rows.keys() != route_ids:
            errors.append("metrics_workload_engineers")
        active_minutes = []
        completions = []
        for route in result.routes:
            service = round(sum(
                max(0.0, (stop.departure - max(
                    stop.service_start, request.planning_time,
                )).total_seconds()) / 60
                for stop in route.stops
            ), 4)
            waiting = round(sum(
                max(0.0, (stop.service_start - max(
                    stop.arrival, request.planning_time,
                )).total_seconds()) / 60
                for stop in route.stops
            ), 4)
            travel = round(route.total_travel_minutes, 4)
            total = round(service + travel + waiting, 4)
            if route.stops:
                active_minutes.append(total)
            completion = max(
                (stop.departure for stop in route.stops
                 if stop.departure > request.planning_time), default=None,
            )
            if completion is not None:
                completions.append(completion)
            row = rows.get(route.engineer_id)
            if row is None:
                continue
            if (row.engineer_name, row.assigned_jobs, row.completion_at) != (
                route.engineer_name, len(route.stops), completion,
            ):
                errors.append("metrics_workload_engineer_details:" + route.engineer_id)
            for field, expected in {
                "service_minutes": service, "travel_minutes": travel,
                "waiting_minutes": waiting, "total_minutes": total,
            }.items():
                if not isclose(getattr(row, field), expected, rel_tol=0, abs_tol=0.00011):
                    errors.append("metrics_workload_" + field + ":" + route.engineer_id)
        completion = max(completions, default=None)
        if workload.completion_at != completion:
            errors.append("metrics_workload_completion_at")
        if workload.active_engineers != len(active_minutes):
            errors.append("metrics_workload_active_engineers")
        for field, expected in {
            "completion_minutes": round(
                (completion - request.planning_time).total_seconds() / 60, 4,
            ) if completion is not None else 0,
            "average_engineer_minutes": round(
                sum(active_minutes) / len(active_minutes), 4,
            ) if active_minutes else 0,
            "max_engineer_minutes": max(active_minutes, default=0),
            "engineer_minutes_stddev": round(
                pstdev(active_minutes), 4,
            ) if len(active_minutes) > 1 else 0,
        }.items():
            actual = getattr(workload, field)
            if actual is None or not isclose(actual, expected, rel_tol=0, abs_tol=0.00011):
                errors.append("metrics_workload_" + field)
    current_stops = {s.job_id: s for r in result.routes for s in r.stops}
    deadline_jobs = [j for j in active.values() if j.sla_deadline is not None]
    met = sum(
        j.id in current_stops and current_stops[j.id].service_start <= j.sla_deadline
        for j in deadline_jobs
    )
    late = sum(
        max(0.0, (current_stops[j.id].service_start - j.sla_deadline).total_seconds() / 60)
        for j in deadline_jobs
        if j.id in current_stops
    )
    if (m.sla_jobs, m.sla_met_jobs) != (len(deadline_jobs), met):
        errors.append("metrics_sla_counts")
    if not isclose(m.sla_rate, met / len(deadline_jobs) if deadline_jobs else 1.0):
        errors.append("metrics_sla_rate")
    if not isclose(m.late_minutes, late, abs_tol=0.051):
        errors.append("metrics_late_minutes")
    if request.previous_plan is not None:
        old_assignments = {jid: eid for jid, (eid, _) in old.items()}
        assignments = {s.job_id: r.engineer_id for r in result.routes for s in r.stops}
        expected_lost = len((old_assignments.keys() & active.keys()) - assignments.keys())
        expected_gained = len(assignments.keys() - old_assignments.keys())
        expected_reassigned = sum(
            assignments[jid] != old_assignments[jid]
            for jid in assignments.keys() & old_assignments.keys()
        )
        if (m.lost_assignments, m.gained_assignments, m.changed_assignments) != (
            expected_lost,
            expected_gained,
            expected_reassigned,
        ):
            errors.append("metrics_assignment_changes")
    return errors
