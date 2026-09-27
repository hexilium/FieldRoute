"""Read-only analysis of one job in an explicit planning snapshot."""

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.models import (
    OptimizationPolicy,
    PlanRequest,
    PlanResult,
    UrgencyPolicy,
    UrgentStartPolicy,
)


class ExplanationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: PlanRequest
    plan: PlanResult
    job_id: str = Field(min_length=1)


class ConstraintEvidence(BaseModel):
    code: str
    job_id: str | None = None
    stage: Literal["eligibility", "source_route", "target_route"] = "target_route"
    message: str
    facts: dict[str, Any] = Field(default_factory=dict)
    positions: int = 1
    example_position: int | None = None


class ObjectiveDifference(BaseModel):
    criterion: str
    current: int | float
    alternative: int | float
    message: str


class AssignmentAlternative(BaseModel):
    engineer_id: str
    engineer_name: str
    is_current_engineer: bool = False
    feasible: bool
    positions_tested: int = 0
    position: int | None = None
    arrival: AwareDatetime | None = None
    service_start: AwareDatetime | None = None
    departure: AwareDatetime | None = None
    leg_distance_km: float | None = None
    leg_travel_minutes: float | None = None
    plan_distance_delta_km: float | None = None
    plan_travel_delta_minutes: float | None = None
    used_engineers_delta: int | None = None
    objective_value: list[int | float] | None = None
    comparison: Literal["better", "equal", "worse", "infeasible", "executing"]
    first_difference: ObjectiveDifference | None = None
    blockers: list[ConstraintEvidence] = Field(default_factory=list)


class JobExplanation(BaseModel):
    job_id: str
    title: str
    chosen_engineer_id: str | None
    optimization_policy: OptimizationPolicy
    urgency_policy: UrgencyPolicy
    urgent_start_policy: UrgentStartPolicy = UrgentStartPolicy.after_primary
    summary: list[str]
    scope: str
    objective_order: list[str]
    current_objective: list[int | float]
    alternatives: list[AssignmentAlternative]
    analysis_time_ms: float
