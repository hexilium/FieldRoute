from __future__ import annotations

from app.domain.planning_settings import ExecutionOptions
from app.domain.compromise_options import CompromiseOptions
from pydantic import model_serializer


from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, model_validator


class JobStatus(StrEnum):
    pending = "pending"
    assigned = "assigned"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"


class Location(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    label: str | None = None


class TimeWindow(BaseModel):
    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def validate_window(self) -> TimeWindow:
        if self.end <= self.start:
            raise ValueError("time window end must be after start")
        return self


# Explicit dispatch areas, not inferred administrative polygons or geocoded addresses.
# IDs are case-sensitive; only leading/trailing whitespace is removed.
DistrictId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class DistrictMode(StrEnum):
    unrestricted = "unrestricted"
    strict = "strict"


class Engineer(BaseModel):
    id: str
    name: str
    start_location: Location
    district_id: DistrictId | None = None
    end_location: Location | None = None
    current_location: Location | None = None
    shift: TimeWindow
    skills: set[str] = Field(default_factory=set)
    equipment: set[str] = Field(default_factory=set)
    transport_modes: set[str] = Field(default_factory=lambda: {"car"})
    travel_mode: Literal["car", "walk", "bicycle", "public_transport"] | None = None
    travel_speed_kmh: float | None = Field(default=None, gt=0, le=200, allow_inf_nan=False)
    max_jobs: int | None = Field(default=None, ge=1)
    available: bool = True
    available_from: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_travel_profile(self) -> Engineer:
        if self.travel_mode is None:
            known = self.transport_modes & {"car", "walk", "bicycle", "public_transport"}
            if len(known) != 1:
                raise ValueError("Укажите travel_mode: способ передвижения на весь маршрут неоднозначен")
            self.travel_mode = next(iter(known))
        if self.travel_mode not in self.transport_modes:
            raise ValueError("travel_mode должен входить в transport_modes инженера")
        return self


class Job(BaseModel):
    id: str
    district_id: DistrictId | None = None
    title: str
    location: Location
    service_minutes: int = Field(default=30, ge=1)
    time_windows: list[TimeWindow] = Field(default_factory=list)
    window_semantics: Literal["start", "completion"] = "start"
    required_skills: set[str] = Field(default_factory=set)
    required_equipment: set[str] = Field(default_factory=set)
    required_transport: str | None = None
    priority: int = Field(default=50, ge=0, le=100)
    sla_deadline: AwareDatetime | None = None
    status: JobStatus = JobStatus.pending
    assigned_engineer_id: str | None = None
    locked: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class ObjectiveWeights(BaseModel):
    model_config = {"extra": "forbid"}
    unassigned: float = Field(default=1000.0, ge=0, le=1_000_000_000, strict=True, allow_inf_nan=False)
    sla_violation: float = Field(default=500.0, ge=0, le=1_000_000_000, strict=True, allow_inf_nan=False)
    overtime: float = Field(default=300.0, ge=0, le=1_000_000_000, strict=True, allow_inf_nan=False)
    travel_minutes: float = Field(default=1.0, ge=0, le=1_000_000_000, strict=True, allow_inf_nan=False)
    distance_km: float = Field(default=0.2, ge=0, le=1_000_000_000, strict=True, allow_inf_nan=False)
    workload_imbalance: float = Field(default=2.0, ge=0, le=1_000_000_000, strict=True, allow_inf_nan=False)
    plan_churn: float = Field(default=20.0, ge=0, le=1_000_000_000, strict=True, allow_inf_nan=False)
    schedule_shift: float = Field(default=0.25, ge=0, le=1_000_000_000, strict=True, allow_inf_nan=False)


class OptimizationPolicy(StrEnum):
    staff_first = "staff_first"
    distance_first = "distance_first"
    sla_first = "sla_first"


class PlanningVariant(StrEnum):
    baseline = "baseline"
    staff_first = "staff_first"
    distance_first = "distance_first"
    sla_first = "sla_first"


class ReplanningProfile(StrEnum):
    full = "full"
    balanced = "balanced"
    conservative = "conservative"


class UrgencyPolicy(StrEnum):
    urgent_first = "urgent_first"
    coverage_first = "coverage_first"


class UrgentStartPolicy(StrEnum):
    after_primary = "after_primary"
    before_primary = "before_primary"
    before_coverage = "before_coverage"


class ServicePriorityPolicy(StrEnum):
    organizer = "organizer"
    numeric = "numeric"


class EmergencyReplanPolicy(StrEnum):
    reroute_future = "reroute_future"
    respect_freeze = "respect_freeze"


class PlanRequest(BaseModel):
    planning_time: AwareDatetime
    engineers: list[Engineer]
    jobs: list[Job]
    district_mode: DistrictMode = DistrictMode.unrestricted
    execution: ExecutionOptions | None = None
    compromise_options: CompromiseOptions | None = None
    optimization_policy: OptimizationPolicy = OptimizationPolicy.staff_first
    urgency_policy: UrgencyPolicy = UrgencyPolicy.urgent_first
    urgent_start_policy: UrgentStartPolicy = UrgentStartPolicy.after_primary
    service_priority_policy: ServicePriorityPolicy = ServicePriorityPolicy.organizer
    emergency_replan_policy: EmergencyReplanPolicy = EmergencyReplanPolicy.reroute_future
    weights: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    freeze_horizon_minutes: int = Field(default=60, ge=0)
    previous_plan: PlanResult | None = None

    @model_serializer(mode="wrap")
    def omit_legacy_options(self, handler):
        result = handler(self)
        for name in ("execution", "compromise_options"):
            if getattr(self, name) is None:
                result.pop(name, None)
        return result

    @model_validator(mode="after")
    def validate_ids_and_references(self) -> PlanRequest:
        engineer_ids = [e.id for e in self.engineers]
        job_ids = [j.id for j in self.jobs]
        if len(engineer_ids) != len(set(engineer_ids)):
            raise ValueError("engineer ids must be unique")
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("job ids must be unique")
        known = set(engineer_ids)
        for job in self.jobs:
            if job.assigned_engineer_id and job.assigned_engineer_id not in known:
                raise ValueError(
                    f"job {job.id} references unknown engineer {job.assigned_engineer_id}"
                )
        running_engineers: set[str] = set()
        old = {
            s.job_id: (r.engineer_id, s)
            for r in (self.previous_plan.routes if self.previous_plan else [])
            for s in r.stops
        }
        engineers = {e.id: e for e in self.engineers}
        for job in self.jobs:
            if job.status != JobStatus.in_progress:
                continue
            if job.id not in old:
                raise ValueError("in_progress job requires its execution interval in previous_plan")
            eid, stop = old[job.id]
            if eid not in engineers or (
                job.assigned_engineer_id and job.assigned_engineer_id != eid
            ):
                raise ValueError("in_progress engineer must match previous_plan")
            if not stop.service_start <= self.planning_time < stop.departure:
                raise ValueError("in_progress interval must contain planning_time")
            if eid in running_engineers:
                raise ValueError("engineer cannot execute two jobs simultaneously")
            if stop.departure > engineers[eid].shift.end:
                raise ValueError("in_progress work must end within the engineer shift")
            if (job.location.lat, job.location.lon) != (stop.location.lat, stop.location.lon):
                raise ValueError("in_progress location must match previous_plan")
            running_engineers.add(eid)
        return self


    @model_validator(mode="after")
    def validate_districts(self) -> PlanRequest:
        if self.district_mode != DistrictMode.strict:
            return self
        active = [j for j in self.jobs if j.status not in {JobStatus.completed, JobStatus.cancelled}]
        missing_e = [e.id for e in self.engineers if e.district_id is None]
        missing_j = [j.id for j in active if j.district_id is None]
        if missing_e or missing_j:
            raise ValueError(
                "Для расчёта по районам укажите district_id: "
                f"инженеры {missing_e[:8]}, активные заявки {missing_j[:8]}. "
                "Район не определяется автоматически по адресу или координатам."
            )
        engineers = {e.id: e for e in self.engineers}
        old = {s.job_id: (r.engineer_id, s)
               for r in (self.previous_plan.routes if self.previous_plan else []) for s in r.stops}
        freeze_until = self.planning_time + timedelta(minutes=self.freeze_horizon_minutes)
        for job in active:
            owner = None
            if job.assigned_engineer_id and (job.locked or job.status in {
                JobStatus.assigned, JobStatus.in_progress,
            }):
                owner = job.assigned_engineer_id
            elif job.id in old:
                eid, stop = old[job.id]
                if (job.status == JobStatus.in_progress or job.locked or (
                    self.freeze_horizon_minutes > 0 and eid in engineers and engineers[eid].available
                    and self.planning_time <= stop.service_start <= freeze_until
                )):
                    owner = eid
            if owner and (owner not in engineers or engineers[owner].district_id != job.district_id):
                raise ValueError(
                    f"Заявка {job.id}: закреплённый/выполняющий инженер {owner} "
                    f"не относится к району {job.district_id}. Строгий режим не снимает "
                    "закрепления и не переносит выполняемую работу. Сначала согласуйте "
                    "районы и закрепления либо оставьте выезды разрешёнными."
                )
        return self


class VariantComparisonRequest(BaseModel):
    request: PlanRequest
    before_variant: PlanningVariant = PlanningVariant.baseline
    after_variant: PlanningVariant = PlanningVariant.staff_first


class PlannedStop(BaseModel):
    job_id: str
    title: str
    location: Location
    arrival: AwareDatetime
    service_start: AwareDatetime
    departure: AwareDatetime
    travel_minutes_from_previous: float
    distance_km_from_previous: float
    explanation: list[str] = Field(default_factory=list)
    frozen: bool = False


class EngineerRoute(BaseModel):
    engineer_id: str
    engineer_name: str
    stops: list[PlannedStop]
    geometry: list[Location] = Field(default_factory=list)
    total_travel_minutes: float = 0
    total_distance_km: float = 0
    work_minutes: float = 0
    overtime_minutes: float = 0


class UnassignedJob(BaseModel):
    job_id: str
    reason_codes: list[str]
    explanation: str


class CandidateEvaluation(BaseModel):
    engineer_id: str
    engineer_name: str
    feasible: bool
    score: float | None = None
    travel_minutes: float | None = None
    distance_km: float | None = None
    service_start: datetime | None = None
    reasons: list[str] = Field(default_factory=list)


class JobDecision(BaseModel):
    job_id: str
    chosen_engineer_id: str | None = None
    frozen: bool = False
    candidates: list[CandidateEvaluation] = Field(default_factory=list)


class LaborMetrics(BaseModel):
    """Planned person-time across engineers, not wall-clock plan duration."""

    service_minutes: float = Field(ge=0, allow_inf_nan=False)
    travel_minutes: float = Field(ge=0, allow_inf_nan=False)
    waiting_minutes: float = Field(ge=0, allow_inf_nan=False)
    total_minutes: float = Field(ge=0, allow_inf_nan=False)
    total_hours: float = Field(ge=0, allow_inf_nan=False)
    scope: Literal["plan", "remaining"] = "plan"


class EngineerWorkload(BaseModel):
    engineer_id: str
    engineer_name: str
    assigned_jobs: int = Field(ge=0)
    service_minutes: float = Field(ge=0, allow_inf_nan=False)
    travel_minutes: float = Field(ge=0, allow_inf_nan=False)
    waiting_minutes: float = Field(ge=0, allow_inf_nan=False)
    total_minutes: float = Field(ge=0, allow_inf_nan=False)
    completion_at: AwareDatetime | None = None


class WorkloadMetrics(BaseModel):
    """Assigned person-time distribution and last-visit completion, not shift capacity."""

    planning_time: AwareDatetime | None = None
    completion_at: AwareDatetime | None = None
    completion_minutes: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    active_engineers: int = Field(ge=0)
    average_engineer_minutes: float = Field(ge=0, allow_inf_nan=False)
    max_engineer_minutes: float = Field(ge=0, allow_inf_nan=False)
    engineer_minutes_stddev: float = Field(ge=0, allow_inf_nan=False)
    engineers: list[EngineerWorkload] = Field(default_factory=list)


class PlanMetrics(BaseModel):
    total_jobs: int
    assigned_jobs: int
    unassigned_jobs: int
    assignment_rate: float
    used_engineers: int = 0
    total_distance_km: float
    total_travel_minutes: float
    # None identifies older snapshots; zero is reserved for an empty workload.
    labor: LaborMetrics | None = None
    workload: WorkloadMetrics | None = None
    overtime_minutes: float
    workload_stddev: float
    changed_assignments: int = 0
    lost_assignments: int = 0
    gained_assignments: int = 0
    reordered_jobs: int = 0
    rescheduled_jobs: int = 0
    schedule_shift_minutes: float = 0
    frozen_jobs: int = 0
    sla_jobs: int = 0
    sla_met_jobs: int = 0
    sla_rate: float = 1.0
    late_minutes: float = 0
    solve_time_ms: float = 0


class PlanJobSnapshot(BaseModel):
    state: Literal["assigned", "unassigned", "completed", "cancelled", "absent"]
    engineer_id: str | None = None
    engineer_name: str | None = None
    position: int | None = None
    arrival: AwareDatetime | None = None
    service_start: AwareDatetime | None = None
    departure: AwareDatetime | None = None
    reason_codes: list[str] = Field(default_factory=list)
    explanation: str | None = None


class PlanJobChange(BaseModel):
    job_id: str
    title: str
    before: PlanJobSnapshot
    after: PlanJobSnapshot
    kinds: list[
        Literal[
            "added",
            "gained_assignment",
            "lost_assignment",
            "reassigned",
            "reordered",
            "rescheduled",
            "completed",
            "cancelled",
            "removed",
            "reason_changed",
        ]
    ]
    start_shift_minutes: float | None = None


class PlanChangeSummary(BaseModel):
    compared_jobs: int = 0
    changed_jobs: int = 0
    unchanged_jobs: int = 0
    added_jobs: int = 0
    gained_assignments: int = 0
    lost_assignments: int = 0
    reassigned_jobs: int = 0
    reordered_jobs: int = 0
    rescheduled_jobs: int = 0
    completed_jobs: int = 0
    cancelled_jobs: int = 0
    removed_jobs: int = 0
    reason_changed_jobs: int = 0


class PlanDiff(BaseModel):
    reference: Literal["previous_plan", "comparison"]
    summary: PlanChangeSummary
    items: list[PlanJobChange]


class MapEngineer(BaseModel):
    engineer_id: str
    name: str
    location: Location
    location_kind: Literal["start", "current", "in_progress"]
    available: bool


class MapJob(BaseModel):
    job_id: str
    title: str
    location: Location
    priority: int
    status: JobStatus


class PlanMapData(BaseModel):
    engineers: list[MapEngineer]
    jobs: list[MapJob]
    distance_model: Literal["haversine_estimate", "osrm_road", "osm_fixed_speed"]


class PlanResult(BaseModel):
    generated_at: datetime
    solver: str
    routes: list[EngineerRoute]
    unassigned: list[UnassignedJob]
    metrics: PlanMetrics
    decisions: list[JobDecision] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    diff: PlanDiff | None = None
    map_data: PlanMapData | None = None


class PlanComparison(BaseModel):
    before: PlanResult
    after: PlanResult
    event: dict[str, Any] = Field(default_factory=dict)
    delta: dict[str, float | int | str] = Field(default_factory=dict)
    diff: PlanDiff | None = None
