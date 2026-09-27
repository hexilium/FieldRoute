"""Compare assignments without counting finished work or shifted indices as churn."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from app.domain.models import (
    Job,
    JobStatus,
    PlanChangeSummary,
    PlanDiff,
    PlanJobChange,
    PlanJobSnapshot,
    PlanRequest,
    PlanResult,
)


def _snapshots(plan: PlanResult) -> tuple[dict[str, PlanJobSnapshot], dict[str, str]]:
    snapshots = {}
    titles = {}
    for route in plan.routes:
        for position, stop in enumerate(route.stops, start=1):
            snapshots[stop.job_id] = PlanJobSnapshot(
                state="assigned",
                engineer_id=route.engineer_id,
                engineer_name=route.engineer_name,
                position=position,
                arrival=stop.arrival,
                service_start=stop.service_start,
                departure=stop.departure,
            )
            titles[stop.job_id] = stop.title
    for item in plan.unassigned:
        snapshots[item.job_id] = PlanJobSnapshot(
            state="unassigned",
            reason_codes=item.reason_codes,
            explanation=item.explanation,
        )
    return snapshots, titles


def _reordered_jobs(
    before: dict[str, PlanJobSnapshot], after: dict[str, PlanJobSnapshot]
) -> set[str]:
    """Flag both participants of each inversion among visits staying with an engineer."""
    groups: dict[str, list[str]] = {}
    for jid, old in before.items():
        new = after.get(jid)
        if (
            old.state == "assigned"
            and new
            and new.state == "assigned"
            and old.engineer_id == new.engineer_id
        ):
            groups.setdefault(old.engineer_id, []).append(jid)
    changed = set()
    for ids in groups.values():
        ids.sort(key=lambda jid: before[jid].position)
        ranks = [after[jid].position for jid in ids]
        # A rank above a later minimum or below an earlier maximum is in an inversion.
        suffix_min = [0] * len(ranks)
        minimum = float("inf")
        for i in range(len(ranks) - 1, -1, -1):
            suffix_min[i] = minimum
            minimum = min(minimum, ranks[i])
        maximum = -1
        for i, jid in enumerate(ids):
            if ranks[i] < maximum or ranks[i] > suffix_min[i]:
                changed.add(jid)
            maximum = max(maximum, ranks[i])
    return changed


def build_plan_diff(
    before: PlanResult,
    after: PlanResult,
    *,
    jobs: list[Job] | None = None,
    reference: Literal["previous_plan", "comparison"] = "comparison",
) -> PlanDiff:
    old, old_titles = _snapshots(before)
    new, new_titles = _snapshots(after)
    titles = {**old_titles, **new_titles}
    if jobs is not None:
        for job in jobs:
            titles[job.id] = job.title
            # Inactive jobs never present in either plan are outside the comparison.
            if job.status in {JobStatus.completed, JobStatus.cancelled} and job.id in old:
                new[job.id] = PlanJobSnapshot(state=job.status.value)
    reordered = _reordered_jobs(old, new)
    ids = old.keys() | new.keys()
    items = []
    for jid in sorted(ids):
        previous = old.get(jid, PlanJobSnapshot(state="absent"))
        current = new.get(jid, PlanJobSnapshot(state="absent"))
        kinds = []
        if previous.state == "absent" and current.state != "absent":
            kinds.append("added")
        if current.state == "absent" and previous.state != "absent":
            kinds.append("removed")
        elif current.state in {"completed", "cancelled"}:
            kinds.append(current.state)
        elif previous.state == "assigned" and current.state == "unassigned":
            kinds.append("lost_assignment")
        elif previous.state != "assigned" and current.state == "assigned":
            kinds.append("gained_assignment")
        elif previous.state == current.state == "assigned":
            if previous.engineer_id != current.engineer_id:
                kinds.append("reassigned")
            if jid in reordered:
                kinds.append("reordered")
            if (previous.arrival, previous.service_start, previous.departure) != (
                current.arrival,
                current.service_start,
                current.departure,
            ):
                kinds.append("rescheduled")
        elif previous.state == current.state == "unassigned" and (
            set(previous.reason_codes) != set(current.reason_codes)
            or previous.explanation != current.explanation
        ):
            kinds.append("reason_changed")
        if not kinds:
            continue
        shift = None
        if previous.service_start is not None and current.service_start is not None:
            shift = round((current.service_start - previous.service_start).total_seconds() / 60, 2)
        items.append(
            PlanJobChange(
                job_id=jid,
                title=titles.get(jid, jid),
                before=previous,
                after=current,
                kinds=kinds,
                start_shift_minutes=shift,
            )
        )
    # Show assignments requiring dispatcher attention before completed/cancelled work.
    severity = {
        "lost_assignment": 0,
        "removed": 1,
        "reassigned": 2,
        "gained_assignment": 3,
        "reordered": 4,
        "rescheduled": 5,
        "reason_changed": 6,
        "added": 7,
        "cancelled": 8,
        "completed": 9,
    }
    items.sort(key=lambda item: (min(severity[k] for k in item.kinds), item.job_id))
    counts = Counter(kind for item in items for kind in item.kinds)
    return PlanDiff(
        reference=reference,
        summary=PlanChangeSummary(
            compared_jobs=len(ids),
            changed_jobs=len(items),
            unchanged_jobs=len(ids) - len(items),
            added_jobs=counts["added"],
            gained_assignments=counts["gained_assignment"],
            lost_assignments=counts["lost_assignment"],
            reassigned_jobs=counts["reassigned"],
            reordered_jobs=counts["reordered"],
            rescheduled_jobs=counts["rescheduled"],
            completed_jobs=counts["completed"],
            cancelled_jobs=counts["cancelled"],
            removed_jobs=counts["removed"],
            reason_changed_jobs=counts["reason_changed"],
        ),
        items=items,
    )


def attach_previous_diff(request: PlanRequest, result: PlanResult) -> PlanResult:
    if request.previous_plan is None or result.diff is not None:
        return result
    diff = build_plan_diff(
        request.previous_plan, result, jobs=request.jobs, reference="previous_plan"
    )
    result.diff = diff
    result.metrics.changed_assignments = diff.summary.reassigned_jobs
    result.metrics.lost_assignments = diff.summary.lost_assignments
    result.metrics.gained_assignments = diff.summary.gained_assignments
    result.metrics.reordered_jobs = diff.summary.reordered_jobs
    result.metrics.rescheduled_jobs = diff.summary.rescheduled_jobs
    return result
