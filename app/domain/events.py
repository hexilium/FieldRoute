"""Events applied to an explicit planning snapshot; no server-side session state."""

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.domain.models import Job, JobStatus, PlanRequest, PlanResult, TimeWindow


class UrgentJob(Job):
    """An emergency arriving during the workday and allowed to trigger replanning."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    service_minutes: int = Field(ge=1)
    time_windows: list[TimeWindow] = Field(min_length=1)
    required_skills: set[str] = Field(min_length=1, max_length=1)
    priority: int = Field(default=100, ge=80, le=100)
    status: Literal[JobStatus.pending] = JobStatus.pending
    assigned_engineer_id: None = None
    locked: Literal[False] = False

class NewJob(Job):
    """An unplanned job added during the shift without forcing urgent priority."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    service_minutes: int = Field(ge=1)
    time_windows: list[TimeWindow] = Field(min_length=1)
    required_skills: set[str] = Field(min_length=1, max_length=1)
    priority: int = Field(default=50, ge=0, le=100)
    status: Literal[JobStatus.pending] = JobStatus.pending
    assigned_engineer_id: None = None
    locked: Literal[False] = False


class Event(BaseModel):
    model_config = ConfigDict(extra="forbid")
    time: AwareDatetime


class UrgentJobEvent(Event):
    type: Literal["urgent_job"]
    job: UrgentJob


class NewJobEvent(Event):
    type: Literal["new_job"]
    job: NewJob


class CancelJobEvent(Event):
    type: Literal["cancel_job"]
    job_id: str = Field(min_length=1)


class EngineerUnavailableEvent(Event):
    type: Literal["engineer_unavailable"]
    engineer_id: str = Field(min_length=1)


class EngineerDelayEvent(Event):
    type: Literal["engineer_delay"]
    engineer_id: str = Field(min_length=1)
    delay_minutes: int = Field(ge=1, strict=True)


class ManualAssignmentEvent(Event):
    type: Literal["manual_assignment"]
    job_id: str = Field(min_length=1)
    engineer_id: str = Field(min_length=1)


PlanningEvent = Annotated[
    UrgentJobEvent
    | NewJobEvent
    | CancelJobEvent
    | EngineerUnavailableEvent
    | EngineerDelayEvent,
    Field(discriminator="type"),
]

HistoryEvent = Annotated[
    UrgentJobEvent
    | NewJobEvent
    | CancelJobEvent
    | EngineerUnavailableEvent
    | EngineerDelayEvent
    | ManualAssignmentEvent,
    Field(discriminator="type"),
]


class EventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: PlanRequest
    plan: PlanResult
    event: PlanningEvent


class EventResult(BaseModel):
    request: PlanRequest
    plan: PlanResult
    event: PlanningEvent
    completed_job_ids: list[str]
    in_progress_job_ids: list[str]
    released_freeze_job_ids: list[str]
    notices: list[str]
