"""Business dispatch priority rules."""
from __future__ import annotations

from app.domain.models import Job, PlanRequest, ServicePriorityPolicy

# Lower is more important. Repair and additional order intentionally share a tier.
ORGANIZER_PRIORITY = {
    "emergency": 0,
    "connection": 1,
    "local": 2,
    "additional_order": 2,
}
PRIORITY_LABELS = {
    0: "Авария",
    1: "Подключение",
    2: "Ремонт / Дозаказ",
    3: "Прочие работы",
}


def service_priority_rank(job: Job) -> int:
    """Infer the organizer work class from the established skill/norm mapping.

    Existing datasets already use emergency/connection/local/additional_order as
    service skills. If several known classes are present, the most important wins.
    Unknown/custom skills remain below the organizer-defined classes instead of
    being guessed into one of them.
    """
    explicit = job.metadata.get("dispatch_work_type") if isinstance(job.metadata, dict) else None
    explicit_rank = {
        "emergency": 0, "connection": 1, "repair": 2,
        "additional_order": 2, "other": 3,
    }.get(explicit)
    if explicit_rank is not None:
        return explicit_rank
    ranks = [ORGANIZER_PRIORITY[skill] for skill in job.required_skills if skill in ORGANIZER_PRIORITY]
    if ranks:
        return min(ranks)
    enrichment = job.metadata.get("enrichment") if isinstance(job.metadata, dict) else None
    record = enrichment.get("service_norm") if isinstance(enrichment, dict) else None
    code = record.get("code") if isinstance(record, dict) else None
    by_norm = {
        "tkd_emergency": 0,
        "connection_basic": 1,
        "local_repair": 2,
        "equipment_order": 2,
    }
    return by_norm.get(code, 3)


def priority_rank(job: Job, request: PlanRequest) -> tuple[int, int]:
    """Return the dispatch order key, retaining numeric priority as a tie-break."""
    if request.service_priority_policy == ServicePriorityPolicy.numeric:
        return 0, -job.priority
    return service_priority_rank(job), -job.priority


def is_emergency(job: Job, request: PlanRequest | None = None) -> bool:
    if request is not None and request.service_priority_policy == ServicePriorityPolicy.numeric:
        return job.priority >= 80
    return service_priority_rank(job) == 0


def has_route_changing_emergency(request: PlanRequest) -> bool:
    """True only for a newly injected emergency explicitly marked as a replan trigger."""
    if request.previous_plan is None:
        return False
    old_ids = {stop.job_id for route in request.previous_plan.routes for stop in route.stops}
    for job in request.jobs:
        if job.id in old_ids or not is_emergency(job, request):
            continue
        if isinstance(job.metadata, dict) and job.metadata.get("route_change_trigger") == "emergency_event":
            return True
    return False


def missing_priority_counts(job_ids: set[str] | frozenset[str], jobs: dict[str, Job]) -> tuple[int, int, int, int]:
    counts = [0, 0, 0, 0]
    for job_id in job_ids:
        counts[min(3, service_priority_rank(jobs[job_id]))] += 1
    return tuple(counts)
