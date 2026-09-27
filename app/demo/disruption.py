from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

from app.domain.models import Job, JobStatus, Location, PlanRequest, PlanResult, TimeWindow
from app.services.metrics import previous_stop_map


def build_disruption_request(base: PlanRequest, morning_plan: PlanResult) -> PlanRequest:
    """Create a realistic mid-shift incident from the morning demo plan."""
    request = deepcopy(base)
    event_time = base.planning_time + timedelta(hours=3, minutes=5)
    request.planning_time = event_time
    request.previous_plan = morning_plan
    request.freeze_horizon_minutes = 60

    old_stops = previous_stop_map(morning_plan)

    # Jobs already finished by the event time should not be replanned.
    for job in request.jobs:
        old = old_stops.get(job.id)
        if old and old.departure <= event_time:
            job.status = JobStatus.completed
        elif old and old.service_start <= event_time < old.departure:
            job.status = JobStatus.in_progress

    # Reconstruct approximate current engineer positions from the published morning plan.
    routes = {route.engineer_id: route for route in morning_plan.routes}
    for engineer in request.engineers:
        route = routes.get(engineer.id)
        if route is None:
            continue
        completed = [stop for stop in route.stops if stop.departure <= event_time]
        in_progress = [
            stop for stop in route.stops if stop.service_start <= event_time < stop.departure
        ]
        if completed:
            engineer.current_location = completed[-1].location
        if in_progress:
            engineer.current_location = in_progress[0].location
            engineer.available_from = in_progress[0].departure

    # A routine engineer is delayed. This makes the event more realistic and forces the
    # optimizer to consider plan changes rather than simply appending one extra stop.
    delayed = next((e for e in request.engineers if e.id == "eng-max"), None)
    if delayed is not None:
        delayed.available_from = max(
            delayed.available_from or event_time,
            event_time + timedelta(minutes=35),
        )

    urgent_window = TimeWindow(start=event_time, end=event_time + timedelta(hours=2))
    request.jobs.append(
        Job(
            id="job-p1",
            title="P1: аварийное восстановление корпоративного канала",
            location=Location(lat=55.7758, lon=37.6556, label="Красносельская"),
            service_minutes=45,
            time_windows=[urgent_window],
            required_skills={"fiber"},
            required_equipment={"otdr"},
            required_transport="car",
            priority=100,
            sla_deadline=event_time + timedelta(minutes=75),
            metadata={"event": "urgent_incident"},
        )
    )
    return request
