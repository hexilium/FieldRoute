from __future__ import annotations

from copy import deepcopy
from enum import StrEnum
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.demo.disruption import build_disruption_request
from app.demo.scenario import demo_request
from app.domain.models import (
    DistrictMode,
    EmergencyReplanPolicy,
    OptimizationPolicy,
    PlanComparison,
    PlanningVariant,
    PlanRequest,
    PlanResult,
    ReplanningProfile,
    ServicePriorityPolicy,
    UrgencyPolicy,
    UrgentStartPolicy,
    VariantComparisonRequest,
)
from app.routing.haversine import ASSUMED_SPEEDS_KMH
from app.services.comparison import compare_plans
from app.services.planner import PlanningService

router = APIRouter(prefix="/api/v1")
SettingsDependency = Annotated[Settings, Depends(get_settings)]

PLANNING_VARIANT_LABELS = {
    PlanningVariant.baseline: "Базовый план",
    PlanningVariant.staff_first: "Меньше инженеров",
    PlanningVariant.distance_first: "Меньше километров",
    PlanningVariant.sla_first: "Соблюдать SLA",
}
REPLANNING_PROFILE_LABELS = {
    ReplanningProfile.full: "Полный пересчёт",
    ReplanningProfile.balanced: "Сбалансированный пересчёт",
    ReplanningProfile.conservative: "Минимум изменений",
}
REPLANNING_PROFILE_SETTINGS = {
    ReplanningProfile.full: {
        "freeze_horizon_minutes": 0,
        "plan_churn": 0.0,
        "schedule_shift": 0.0,
    },
    ReplanningProfile.balanced: {
        "freeze_horizon_minutes": 30,
        "plan_churn": 20.0,
        "schedule_shift": 0.25,
    },
    # This is the former hard-coded stable variant. Keeping these exact values
    # preserves the default demo comparison while making the profile selectable.
    ReplanningProfile.conservative: {
        "freeze_horizon_minutes": 60,
        "plan_churn": 35.0,
        "schedule_shift": 0.35,
    },
}


def _variant_metadata(before: StrEnum, after: StrEnum, labels: dict) -> dict:
    return {
        "variant_ids": [before.value, after.value],
        "variant_labels": [labels[before], labels[after]],
    }


def _ensure_distinct_variants(before: StrEnum, after: StrEnum) -> None:
    if before == after:
        raise HTTPException(status_code=422, detail="Выберите два разных варианта для сравнения.")


@router.get("/health")
async def health(settings: SettingsDependency) -> dict:
    return {
        "status": "ok",
        "solver_backend": settings.solver_backend,
        "routing_backend": settings.routing_backend,
    }


@router.get("/capabilities")
async def capabilities(settings: SettingsDependency) -> dict:
    return {
        "districts": {
            "modes": ["strict", "unrestricted"], "default": "unrestricted",
            "field": "district_id", "solver_backends": ["insertion", "baseline"],
            "strict_requires_complete_labels": True, "automatic_geocoding": False,
            "strict_strategy": "independent_districts", "budget_scope": "per_request_per_policy",
        },
        "hard_constraints": [
            "district_membership_in_strict_mode",
            "skills",
            "equipment",
            "transport",
            "shift",
            "time_windows",
            "engineer_availability",
            "locked_assignment",
            "freeze_horizon",
        ],
        "soft_objectives": [
            "assigned_jobs",
            "used_engineers",
            "sla",
            "priority",
            "travel_time",
            "distance",
            "plan_churn",
            "schedule_shift",
        ],
        "routing": ["haversine", "osrm", "local_roads"],
        "solvers": ["insertion", "baseline", "heuristic", "vroom"],
        "planning_variants": [variant.value for variant in PlanningVariant],
        "replanning_profiles": [profile.value for profile in ReplanningProfile],
        "optimization_policies": [p.value for p in OptimizationPolicy],
        "urgency_policies": [p.value for p in UrgencyPolicy],
        "urgent_start_policies": [p.value for p in UrgentStartPolicy],
        "service_priority_policies": [p.value for p in ServicePriorityPolicy],
        "emergency_replan_policies": [p.value for p in EmergencyReplanPolicy],
        "organizer_service_priority": ["emergency", "connection", "repair_or_additional_order"],
        "window_semantics": ["start", "completion"],
        "policy_backends": ["insertion", "baseline"],
        "travel_profiles": {
            "assumed_speeds_kmh": ASSUMED_SPEEDS_KMH,
            "custom_speed_field": "travel_speed_kmh",
            "routing_backend": "haversine",
            "routing_backends": ["haversine", "local_roads"],
            "solver_backends": ["insertion", "baseline"],
            "mode_fixed_per_engineer": True,
        },
        "events": [
            "urgent_job",
            "new_job",
            "cancel_job",
            "engineer_unavailable",
            "engineer_delay",
        ],
        "event_solver": "insertion",
        "compromise": {
            "neighborhoods": ["basic", "extended"], "default_neighborhood": "basic",
            "compound_operators": ["free_swap", "tail_exchange"], "basic_prefix_attempts": 30000,
            "endpoint": "/api/v1/compromise", "stream": "/api/v1/compromise/stream",
            "same_assigned_jobs": True, "default_attempt_limit": 30000,
            "max_attempt_limit": 200000, "reference_required": True,
            "limits_persist_on_replan": False,
        },
        "search_strategies": {
            "available": ["full", "adaptive"], "selected": settings.search_strategy,
            "attempt_limit": settings.search_attempt_limit,
            "slice_attempts": settings.search_slice_attempts,
            "attempt_limit_applies_to": "adaptive",
            "adaptive_workers": 1, "adaptive_shared_policy_pool": False,
        },
        "search_budget": {
            "setting": "search_time_limit_ms", "limit_ms": settings.search_time_limit_ms,
            "solver_backends": ["insertion"], "routing_backends": ["haversine", "local_roads"],
            "scope": "cooperative_search; matrix_preparation_finalization_and_validation_excluded",
        },
        "explanations": {"endpoint": "/api/v1/explanations/job", "scope": "single_job_reinsertion", "read_only": True},
    }


@router.post("/plan", response_model=PlanResult)
async def plan(payload: PlanRequest, settings: SettingsDependency) -> PlanResult:
    return await PlanningService(settings).plan(payload)


@router.post("/compare", response_model=PlanComparison)
async def compare_baseline(payload: PlanRequest, settings: SettingsDependency) -> PlanComparison:
    baseline = await PlanningService(
        settings.model_copy(update={"solver_backend": "baseline"})
    ).plan(payload)
    improved = await PlanningService(
        settings.model_copy(update={"solver_backend": "insertion"})
    ).plan(payload)
    return compare_plans(
        baseline,
        improved,
        event={
            "comparison": "fifo_baseline_vs_insertion",
            "optimization_policy": payload.optimization_policy.value,
            "urgency_policy": payload.urgency_policy.value,
            "urgent_start_policy": payload.urgent_start_policy.value,
        },
    )


@router.post("/variants/compare", response_model=PlanComparison)
async def compare_variants(
    payload: VariantComparisonRequest,
    settings: SettingsDependency,
) -> PlanComparison:
    """Compare any two planning variants on one request.

    Optimized policies share one insertion candidate search. The FIFO baseline is
    calculated separately because it is a different algorithm rather than a policy.
    """
    request = payload.request
    before, after = payload.before_variant, payload.after_variant
    _ensure_distinct_variants(before, after)
    selected = (before, after)
    policies = tuple(
        OptimizationPolicy(variant.value)
        for variant in selected
        if variant != PlanningVariant.baseline
    )
    plans: dict[PlanningVariant, PlanResult] = {}
    if PlanningVariant.baseline in selected:
        # FIFO routes do not depend on the optimization policy, but evaluating the
        # baseline under the selected optimized policy keeps diagnostics meaningful.
        baseline_policy = policies[0] if policies else request.optimization_policy
        baseline_request = request.model_copy(update={"optimization_policy": baseline_policy})
        plans[PlanningVariant.baseline] = await PlanningService(
            settings.model_copy(update={"solver_backend": "baseline"})
        ).plan(baseline_request)
    if policies:
        optimized = await PlanningService(
            settings.model_copy(update={"solver_backend": "insertion"})
        ).plan_policies(request, policies)
        plans.update({PlanningVariant(policy.value): plan for policy, plan in optimized.items()})

    return compare_plans(
        plans[before],
        plans[after],
        event={
            "comparison": "planning_variants",
            "comparison_title": (
                "Эффект оптимизации"
                if PlanningVariant.baseline in selected
                else "Цена соблюдения сроков"
            ),
            **_variant_metadata(before, after, PLANNING_VARIANT_LABELS),
            "urgency_policy": request.urgency_policy.value,
            "urgent_start_policy": request.urgent_start_policy.value,
        },
    )


@router.get("/demo/comparison", response_model=PlanComparison)
async def demo_comparison(
    settings: SettingsDependency,
    optimization_policy: OptimizationPolicy = OptimizationPolicy.staff_first,
    urgent_start_policy: UrgentStartPolicy = UrgentStartPolicy.after_primary,
) -> PlanComparison:
    payload = demo_request()
    payload.optimization_policy = optimization_policy
    payload.urgent_start_policy = urgent_start_policy
    return await compare_baseline(payload, settings)


@router.post("/policies/compare", response_model=PlanComparison)
async def compare_policies(payload: PlanRequest, settings: SettingsDependency) -> PlanComparison:
    service = PlanningService(settings.model_copy(update={"solver_backend": "insertion"}))
    plans = await service.plan_policies(
        payload,
        (OptimizationPolicy.staff_first, OptimizationPolicy.sla_first),
    )
    return compare_plans(
        plans[OptimizationPolicy.staff_first],
        plans[OptimizationPolicy.sla_first],
        event={
            "comparison": "optimization_policies",
            "urgent_start_policy": payload.urgent_start_policy.value,
        },
    )


@router.get("/demo/policies", response_model=PlanComparison)
async def demo_policies(
    settings: SettingsDependency,
    urgent_start_policy: UrgentStartPolicy = UrgentStartPolicy.after_primary,
) -> PlanComparison:
    payload = demo_request()
    payload.urgent_start_policy = urgent_start_policy
    return await compare_policies(payload, settings)


@router.post("/replan", response_model=PlanResult)
async def replan(payload: PlanRequest, settings: SettingsDependency) -> PlanResult:
    # Same contract by design: previous_plan + updated jobs/engineers drive replanning.
    return await PlanningService(settings).plan(payload)


@router.get("/demo/plan", response_model=PlanResult)
async def demo_plan(
    settings: SettingsDependency,
    optimization_policy: OptimizationPolicy = OptimizationPolicy.staff_first,
    urgent_start_policy: UrgentStartPolicy = UrgentStartPolicy.after_primary,
) -> PlanResult:
    payload = demo_request()
    payload.optimization_policy = optimization_policy
    payload.urgent_start_policy = urgent_start_policy
    return await PlanningService(settings).plan(payload)


@router.get("/demo/request", response_model=PlanRequest)
async def demo_source() -> PlanRequest:
    return demo_request()


@router.get("/demo/replanning", response_model=PlanComparison)
async def demo_replanning(
    settings: SettingsDependency,
    optimization_policy: OptimizationPolicy = OptimizationPolicy.staff_first,
    urgency_policy: UrgencyPolicy = UrgencyPolicy.urgent_first,
    urgent_start_policy: UrgentStartPolicy = UrgentStartPolicy.after_primary,
    before_variant: ReplanningProfile = ReplanningProfile.full,
    after_variant: ReplanningProfile = ReplanningProfile.conservative,
) -> PlanComparison:
    """Compare two replanning profiles for the same incident."""
    _ensure_distinct_variants(before_variant, after_variant)
    service = PlanningService(settings)
    base = demo_request()
    base.optimization_policy = optimization_policy
    base.urgency_policy = urgency_policy
    base.urgent_start_policy = urgent_start_policy
    morning = await service.plan(base)
    event_request = build_disruption_request(base, morning)

    variants = []
    for profile in (before_variant, after_variant):
        variant = deepcopy(event_request)
        profile_settings = REPLANNING_PROFILE_SETTINGS[profile]
        variant.freeze_horizon_minutes = profile_settings["freeze_horizon_minutes"]
        variant.weights.plan_churn = profile_settings["plan_churn"]
        variant.weights.schedule_shift = profile_settings["schedule_shift"]
        variants.append(await service.plan(variant))
    legacy_pair = (
        before_variant == ReplanningProfile.full
        and after_variant == ReplanningProfile.conservative
    )
    return compare_plans(
        variants[0],
        variants[1],
        event={
            "type": "urgent_incident_and_delay",
            "time": event_request.planning_time.isoformat(),
            "urgent_job_id": "job-p1",
            "delayed_engineer_id": "eng-max",
            "delay_minutes": 35,
            "comparison": (
                "naive_full_recalculation_vs_stable_replanning"
                if legacy_pair
                else "replanning_profiles"
            ),
            "comparison_title": "Устойчивость к событиям",
            **_variant_metadata(
                before_variant, after_variant, REPLANNING_PROFILE_LABELS,
            ),
            "profile_settings": {
                profile.value: REPLANNING_PROFILE_SETTINGS[profile]
                for profile in (before_variant, after_variant)
            },
            "optimization_policy": optimization_policy.value,
            "urgency_policy": urgency_policy.value,
            "urgent_start_policy": urgent_start_policy.value,
        },
    )


@router.post("/districts/compare", response_model=PlanComparison)
async def compare_districts(payload: PlanRequest, settings: SettingsDependency) -> PlanComparison:
    """Both modes use the same source, objective and request-level budget settings."""
    from pydantic import ValidationError

    try:
        strict = PlanRequest.model_validate(payload.model_dump() | {"district_mode": "strict"})
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="; ".join(e["msg"] for e in exc.errors()[:5])) from exc
    unrestricted = strict.model_copy(update={"district_mode": DistrictMode.unrestricted}, deep=True)
    service = PlanningService(settings.model_copy(update={"solver_backend": "insertion"}))
    before, after = await service.plan(strict), await service.plan(unrestricted)
    return compare_plans(before, after, event={
        "comparison": "district_modes", "comparison_title": "Эффект выездов за пределы района",
        "variant_ids": ["strict", "unrestricted"],
        "variant_labels": ["Без выездов из района", "Выезды разрешены"],
        "variant_district_modes": ["strict", "unrestricted"],
        "same_input": True, "optimality_proven": False,
    })


@router.get("/demo/districts/request", response_model=PlanRequest)
async def district_demo() -> PlanRequest:
    from app.demo.districts import district_demo_request

    return district_demo_request()


@router.get("/demo/districts/small", response_model=PlanRequest)
async def small_district_demo() -> PlanRequest:
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / 'demo/data/districts_small.request.json'
    return PlanRequest.model_validate_json(path.read_text(encoding='utf-8'))
