"""Portable, versioned snapshot of a working plan and its event history."""

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_serializer, model_validator

from app.domain.events import HistoryEvent
from app.domain.models import PlanRequest, PlanResult


class StateOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    request: PlanRequest


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=300)


class EventTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=300)
    service_minutes: int = Field(ge=1, le=1440, strict=True)
    skill: str = Field(min_length=1, max_length=100)
    priority: int = Field(ge=0, le=100, strict=True)
    equipment: list[str] = Field(default_factory=list, max_length=100)


class WorkspaceCatalogs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skills: list[CatalogEntry] = Field(min_length=1, max_length=200)
    equipment: list[CatalogEntry] = Field(default_factory=list, max_length=200)
    templates: list[EventTemplate] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def validate_ids_and_references(self) -> "WorkspaceCatalogs":
        skill_ids = [item.id for item in self.skills]
        equipment_ids = [item.id for item in self.equipment]
        template_ids = [item.id for item in self.templates]
        for name, values in (
            ("навыков", skill_ids),
            ("оборудования", equipment_ids),
            ("шаблонов", template_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"Коды {name} не должны повторяться.")
        known_skills = set(skill_ids)
        known_equipment = set(equipment_ids)
        for template in self.templates:
            if template.skill not in known_skills:
                raise ValueError("Шаблон ссылается на неизвестный навык.")
            if not set(template.equipment) <= known_equipment:
                raise ValueError("Шаблон ссылается на неизвестное оборудование.")
        return self


class SavedState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    format: Literal["fieldroute-state"] = "fieldroute-state"
    format_version: Literal[1] = 1
    name: str = Field(default="Загруженный сценарий", min_length=1, max_length=300)
    saved_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    request: PlanRequest
    plan: PlanResult
    events: list[HistoryEvent] = Field(default_factory=list, max_length=1000)
    origin: StateOrigin | None = None
    # Explicit UI provenance prevents a custom scenario named "Демо" from being
    # mistaken for the built-in dataset after restore.
    dataset_kind: Literal["demo", "custom"] | None = None
    # Workspace-local labels and event templates. They never alter solver inputs.
    catalogs: WorkspaceCatalogs | None = None

    @model_serializer(mode="wrap")
    def omit_missing_catalogs(self, handler):
        value = handler(self)
        for field in ("catalogs", "dataset_kind"):
            if getattr(self, field) is None:
                value.pop(field, None)
        return value

    @model_validator(mode="before")
    @classmethod
    def require_version_or_legacy(cls, value: Any) -> Any:
        if isinstance(value, dict) and "format" not in value and "format_version" not in value:
            if not set(value) <= {"request", "plan", "events"}:
                raise ValueError("Укажите format и format_version для нового формата состояния.")
        elif isinstance(value, dict) and not {"format", "format_version"} <= value.keys():
            raise ValueError("Нужны оба поля: format и format_version.")
        return value
