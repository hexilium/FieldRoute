"""A preview of one explicit dispatcher decision on a published plan."""

from pydantic import BaseModel, ConfigDict, Field

from app.domain.events import ManualAssignmentEvent
from app.domain.explanations import ConstraintEvidence
from app.domain.models import PlanRequest, PlanResult


class AssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request: PlanRequest
    plan: PlanResult
    job_id: str = Field(min_length=1)
    engineer_id: str = Field(min_length=1)


class AssignmentPreview(BaseModel):
    accepted: bool
    request: PlanRequest | None = None
    plan: PlanResult | None = None
    event: ManualAssignmentEvent | None = None
    blockers: list[ConstraintEvidence] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
    scope: str
