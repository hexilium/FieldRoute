"""Explicit, request-local limits for improving an already reviewed plan."""
from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import PlanRequest, PlanResult
from app.domain.compromise_options import CompromiseOptions


class CompromiseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request: PlanRequest
    reference_plan: PlanResult
    options: CompromiseOptions = Field(default_factory=CompromiseOptions)
    reference_label: str = Field(default="Исходный план", min_length=1, max_length=100)
