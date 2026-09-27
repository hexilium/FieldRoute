"""Validate the reference, improve it within explicit limits, return a comparison."""

from time import perf_counter

from app.config import Settings
from app.domain.compromise import CompromiseRequest
from app.domain.models import OptimizationPolicy, PlanComparison, PlanRequest
from app.domain.priorities import is_emergency
from app.services.comparison import compare_plans
from app.services.planner import PlanningService
from app.services.planning_settings import effective_settings
from app.services.progress import current_progress
from app.services.validation import validate_plan
from app.solvers.compromise import CompromiseSearch


class InvalidCompromise(ValueError):
    pass


async def plan_compromise(payload: CompromiseRequest, settings: Settings) -> PlanComparison:
    started = perf_counter()
    payload = CompromiseRequest.model_validate(payload.model_dump())
    # Copy and validate, never edit the client's request or the reference plan.
    request = PlanRequest.model_validate(payload.request.model_dump())
    request.optimization_policy = OptimizationPolicy.sla_first
    published = payload.reference_plan
    settings = effective_settings(settings, request)
    service = PlanningService(settings)
    service._validate_request(request)
    expected = {"haversine": "haversine_estimate", "local_roads": "osm_fixed_speed", "osrm": "osrm_road"}
    if published.map_data and published.map_data.distance_model != expected[settings.routing_backend]:
        raise InvalidCompromise("Модель расстояний исходного плана отличается от настроек сервера. Пересчитайте исходный план.")
    if settings.routing_backend == "osrm" and settings.search_time_limit_ms is not None:
        raise InvalidCompromise("Лимит времени поиска для OSRM не поддерживается. Отключите search_time_limit_ms.")
    progress = current_progress.get()
    if progress is not None:
        progress.begin(sum(j.status not in {"completed", "cancelled"} for j in request.jobs), "compromise")
        progress.update(force=True, search_strategy="compromise", attempt_limit=payload.options.attempt_limit)
    routing = service.routing_provider()
    await routing.prepare(request)
    errors = await validate_plan(request, published, routing)
    if errors:
        raise InvalidCompromise("Исходный план не соответствует запросу: " + ", ".join(errors[:8]))
    search = CompromiseSearch(request, routing)
    # Restores exact published starts/extra waiting; costs are independently
    # recomputed with this routing provider instead of trusting UI rounding.
    reference = await search.evaluator.published_routes(published)
    search.initialize(reference, payload.options)
    report = await search.optimize_reference(time_limit_ms=settings.search_time_limit_ms)
    result = await search.build_result(published, started, report)
    service._decorate(request, result)
    errors = validate_limits(request, published, result, report["limits"])
    if errors:
        raise RuntimeError("Compromise output violates limits: " + ", ".join(errors))
    report["validation"] = "passed"
    result.metrics.solve_time_ms = round((perf_counter() - started) * 1000, 1)
    return compare_plans(
        published.model_copy(deep=True), result,
        event={
            "comparison": "resource_limited_sla", "comparison_title": "SLA в пределах ресурсов",
            "variant_ids": ["reference", "compromise"],
            "variant_labels": [payload.reference_label, "Компромисс по SLA"],
            "limits_apply_to": "this_comparison_only",
        },
    )


def validate_limits(request, reference, result, limits) -> list[str]:
    """A second check on published stops, without using search scores/summaries.

    validate_plan already verifies the distances against the routing provider.
    PlanMetrics rounds to 2/1 decimals, so do not use it at a hard boundary.
    """
    errors = []
    old_ids = {s.job_id for r in reference.routes for s in r.stops}
    stops = [s for r in result.routes for s in r.stops]
    if {s.job_id for s in stops} != old_ids or len(stops) != len(old_ids):
        errors.append("same_assigned_jobs")
    if sum(bool(r.stops) for r in result.routes) > limits["max_used_engineers"]:
        errors.append("max_used_engineers")
    if sum(r.total_distance_km for r in result.routes) > limits["max_distance_km"] + 1e-9:
        errors.append("max_distance_km")
    jobs = {j.id: j for j in request.jobs}
    met, late, urgent = 0, 0.0, 0.0
    for stop in stops:
        job = jobs[stop.job_id]
        if job.sla_deadline is not None:
            met += stop.service_start <= job.sla_deadline
            late += max(0.0, (stop.service_start - job.sla_deadline).total_seconds() / 60)
        if is_emergency(job, request) and job.status != "in_progress":
            urgent += max(0.0, (stop.service_start - request.planning_time).total_seconds() / 60)
    if met < limits["min_sla_met_jobs"]:
        errors.append("min_sla_met_jobs")
    if late > limits["max_late_minutes"] + 1e-9:
        errors.append("max_late_minutes")
    if limits["max_urgent_start_minutes"] is not None and urgent > limits["max_urgent_start_minutes"] + 1e-9:
        errors.append("max_urgent_start_minutes")
    return errors
