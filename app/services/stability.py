from __future__ import annotations

from datetime import timedelta

from app.domain.models import EmergencyReplanPolicy, Job, JobStatus, PlannedStop, PlanRequest
from app.domain.priorities import has_route_changing_emergency
from app.services.metrics import previous_assignment_map, previous_stop_map


def forced_assignment(
    job: Job,
    request: PlanRequest,
    previous_assignment: dict[str, str] | None = None,
    old_stops: dict[str, PlannedStop] | None = None,
) -> tuple[str | None, bool, str | None]:
    """Return (engineer_id, is_frozen, reason) for hard plan-stability constraints."""
    previous_assignment = previous_assignment or previous_assignment_map(request.previous_plan)
    old_stops = old_stops or previous_stop_map(request.previous_plan)

    if job.assigned_engineer_id and (
        job.locked or job.status in {JobStatus.assigned, JobStatus.in_progress}
    ):
        return job.assigned_engineer_id, True, "explicit_lock"

    old_engineer = previous_assignment.get(job.id)
    old_stop = old_stops.get(job.id)
    if job.locked and old_engineer:
        return old_engineer, True, "job_lock"

    owner_available = any(e.id == old_engineer and e.available for e in request.engineers)
    emergency_override = (
        request.emergency_replan_policy == EmergencyReplanPolicy.reroute_future
        and has_route_changing_emergency(request)
    )
    if old_engineer and old_stop and owner_available and request.freeze_horizon_minutes > 0 and not emergency_override:
        freeze_until = request.planning_time + timedelta(minutes=request.freeze_horizon_minutes)
        if request.planning_time <= old_stop.service_start <= freeze_until:
            return old_engineer, True, "freeze_horizon"

    return None, False, None
