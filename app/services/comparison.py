from __future__ import annotations

from app.domain.models import PlanComparison, PlanResult
from app.services.plan_diff import build_plan_diff


def compare_plans(before: PlanResult, after: PlanResult, event: dict) -> PlanComparison:
    b = before.metrics
    a = after.metrics
    delta = {
        "assigned_jobs_delta": a.assigned_jobs - b.assigned_jobs,
        "used_engineers_delta": a.used_engineers - b.used_engineers,
        "changed_assignments_saved": b.changed_assignments - a.changed_assignments,
        "lost_assignments_saved": b.lost_assignments - a.lost_assignments,
        "reordered_jobs_saved": b.reordered_jobs - a.reordered_jobs,
        "schedule_shift_saved_minutes": round(
            b.schedule_shift_minutes - a.schedule_shift_minutes, 1
        ),
        "distance_delta_km": round(a.total_distance_km - b.total_distance_km, 2),
        "travel_delta_minutes": round(a.total_travel_minutes - b.total_travel_minutes, 1),
        "sla_rate_delta_pp": round((a.sla_rate - b.sla_rate) * 100, 1),
        "sla_met_jobs_delta": a.sla_met_jobs - b.sla_met_jobs,
        "late_minutes_delta": round(a.late_minutes - b.late_minutes, 1),
        "stable_solve_time_ms": a.solve_time_ms,
    }
    if b.labor is not None and a.labor is not None:
        minutes = a.labor.total_minutes - b.labor.total_minutes
        delta["labor_minutes_delta"] = round(minutes, 4)
        # Convert the minute difference once, avoiding subtraction of rounded hours.
        delta["labor_hours_delta"] = round(minutes / 60, 4)
        for component in ("service", "travel", "waiting"):
            field = component + "_minutes"
            delta["labor_" + field + "_delta"] = round(
                getattr(a.labor, field) - getattr(b.labor, field), 4,
            )
    if b.workload is not None and a.workload is not None:
        delta["max_engineer_minutes_delta"] = round(
            a.workload.max_engineer_minutes - b.workload.max_engineer_minutes, 4,
        )
        if (
            a.workload.planning_time == b.workload.planning_time
            and a.workload.completion_minutes is not None
            and b.workload.completion_minutes is not None
        ):
            delta["completion_minutes_delta"] = round(
                a.workload.completion_minutes - b.workload.completion_minutes, 4,
            )
    return PlanComparison(
        before=before,
        after=after,
        event=event,
        delta=delta,
        diff=build_plan_diff(before, after, reference="comparison"),
    )
