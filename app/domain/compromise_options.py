"""Options shared by the compromise request and portable workspace defaults."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class CompromiseOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    extra_distance_percent: float = Field(default=10.0, ge=0, le=200, allow_inf_nan=False)
    extra_engineers: int = Field(default=0, ge=0, le=10000, strict=True)
    # Omitted in existing v20 clients: preserve their original search trajectory.
    neighborhood: Literal["basic", "extended"] = "basic"
    # None maximizes SLA; a target allows reducing resources once the target is met.
    target_sla_percent: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    attempt_limit: int = Field(default=30000, ge=0, le=200000, strict=True)


    @field_validator("extra_distance_percent", "target_sla_percent", mode="before")
    @classmethod
    def no_boolean_percent(cls, value):
        if isinstance(value, bool):
            raise ValueError("Процент должен быть числом, не логическим значением")
        return value

