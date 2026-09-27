"""Snapshot the request's active points independently of solver assignments."""

from app.domain.models import JobStatus, MapEngineer, MapJob, PlanMapData, PlanRequest


def build_map_data(
    request: PlanRequest, *, road_distances: bool, local_roads: bool = False,
) -> PlanMapData:
    jobs = [
        job for job in request.jobs
        if job.status not in {JobStatus.completed, JobStatus.cancelled}
    ]
    previous_engineers = {
        stop.job_id: route.engineer_id
        for route in (request.previous_plan.routes if request.previous_plan else [])
        for stop in route.stops
    }
    # Execution determines the origin even when current_location is stale or absent.
    executing = {
        previous_engineers[job.id]: job.location
        for job in jobs if job.status == JobStatus.in_progress
    }
    engineers = []
    for engineer in request.engineers:
        if engineer.id in executing:
            location, kind = executing[engineer.id], "in_progress"
        elif engineer.current_location is not None:
            location, kind = engineer.current_location, "current"
        else:
            location, kind = engineer.start_location, "start"
        engineers.append(MapEngineer(
            engineer_id=engineer.id,
            name=engineer.name,
            location=location,
            location_kind=kind,
            available=engineer.available,
        ))
    return PlanMapData(
        engineers=engineers,
        jobs=[MapJob(
            job_id=job.id,
            title=job.title,
            location=job.location,
            priority=job.priority,
            status=job.status,
        ) for job in jobs],
        distance_model=("osm_fixed_speed" if local_roads else
                        "osrm_road" if road_distances else "haversine_estimate"),
    )
