"""District reporting uses job destination for visits and home district for staff.

No point-in-polygon inference. An unknown district is never a shared service area.
A visiting engineer can appear in several destination rows; rows must not be summed
as a headcount. The home-used headcount, on the other hand, is additive.
"""
from __future__ import annotations

from app.domain.models import DistrictMode, JobStatus, PlanRequest, PlanResult


def attach_district_summary(request: PlanRequest, result: PlanResult) -> PlanResult:
    active = {j.id: j for j in request.jobs
              if j.status not in {JobStatus.completed, JobStatus.cancelled}}
    if request.district_mode == DistrictMode.unrestricted and not any(
        x.district_id is not None for x in [*request.engineers, *active.values()]
    ):
        # Preserve old result semantics for unlabeled snapshots.
        result.diagnostics.pop("districts", None)
        return result
    engineers = {e.id: e for e in request.engineers}
    ids = {e.district_id for e in request.engineers} | {j.district_id for j in active.values()}
    rows = {}
    for district in sorted(ids, key=lambda x: (x is None, x or "")):
        jobs = [j for j in active.values() if j.district_id == district]
        staff = [e for e in request.engineers if e.district_id == district]
        rows[district] = dict(
            district_id=district, total_jobs=len(jobs), assigned_jobs=0, unassigned_jobs=0,
            sla_jobs=sum(j.sla_deadline is not None for j in jobs), sla_met_jobs=0,
            engineers=len(staff), available_engineers=sum(e.available for e in staff),
            used_home_engineers=0, serving_engineer_ids=set(), visiting_engineer_ids=set(),
            inbound_jobs=0, outbound_jobs=0, unclassified_assigned_jobs=0,
            total_distance_km=0.0, total_travel_minutes=0.0, late_minutes=0.0,
        )
    cross_jobs = unknown_jobs = 0
    for route in result.routes:
        engineer = engineers.get(route.engineer_id)
        if engineer is None:
            continue  # Result validity is checked separately, never hidden by this report.
        if route.stops:
            rows[engineer.district_id]["used_home_engineers"] += 1
        for stop in route.stops:
            job = active.get(stop.job_id)
            if job is None:
                continue
            row = rows[job.district_id]
            row["assigned_jobs"] += 1
            row["serving_engineer_ids"].add(engineer.id)
            row["total_distance_km"] += stop.distance_km_from_previous
            row["total_travel_minutes"] += stop.travel_minutes_from_previous
            if job.sla_deadline is not None:
                row["sla_met_jobs"] += stop.service_start <= job.sla_deadline
                row["late_minutes"] += max(0.0, (stop.service_start-job.sla_deadline).total_seconds()/60)
            if job.district_id is None or engineer.district_id is None:
                unknown_jobs += 1
                row["unclassified_assigned_jobs"] += 1
            elif job.district_id != engineer.district_id:
                cross_jobs += 1
                row["visiting_engineer_ids"].add(engineer.id)
                row["inbound_jobs"] += 1
                rows[engineer.district_id]["outbound_jobs"] += 1
    for row in rows.values():
        row["unassigned_jobs"] = row["total_jobs"] - row["assigned_jobs"]
        row["sla_rate"] = row["sla_met_jobs"] / row["sla_jobs"] if row["sla_jobs"] else None
        for name in ("serving_engineer_ids", "visiting_engineer_ids"):
            row[name] = sorted(row[name])
        for name in ("total_distance_km", "total_travel_minutes", "late_minutes"):
            row[name] = round(row[name], 6)
    result.diagnostics["districts"] = {
        "mode": request.district_mode.value, "cross_district_jobs": cross_jobs,
        "unclassified_assigned_jobs": unknown_jobs, "rows": list(rows.values()),
        "distance_attribution": "incoming_leg_to_job_destination; no_return_trip",
        "headcount_attribution": "used_home_engineers_additive; serving_engineers_not_additive",
        "boundary_scope": "job_membership_only; travel_may_cross_boundaries",
    }
    return result
