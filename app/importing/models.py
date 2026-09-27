from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.domain.models import DistrictId, DistrictMode, Engineer, Location, PlanRequest, TimeWindow
from app.importing.norms import SERVICE_NORMS, ServiceNormCode

Skill = Literal["local", "connection", "additional_order", "emergency"]
Equipment = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Transport = Literal["car", "walk", "bicycle", "public_transport"]
Priority = Literal["normal", "urgent"]

SKILLS = {
    "local": "Локальные работы",
    "connection": "Подключения",
    "additional_order": "Дозаказы",
    "emergency": "Аварийные работы",
}
TRANSPORTS = {
    "car": "Автомобиль",
    "walk": "Пешеход",
    "bicycle": "Велосипед",
    "public_transport": "Общественный транспорт",
}
PRIORITIES = {"normal": 50, "urgent": 100}


def key(value: str) -> str:
    """Normalize only whitespace and case, never merge distinct address spellings."""
    return " ".join(value.split()).casefold()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ImportEngineer(StrictModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    start_location: Location
    district_id: DistrictId | None = None
    shift: TimeWindow
    skills: set[Skill] = Field(min_length=1, max_length=4)
    equipment: set[Equipment] = Field(default_factory=set)
    max_jobs: int | None = Field(default=None, ge=1, strict=True)
    transport: Transport
    travel_speed_kmh: float | None = Field(default=None, gt=0, le=200, allow_inf_nan=False)

    def to_engineer(self) -> Engineer:
        return Engineer(
            id=self.id,
            name=self.name,
            start_location=self.start_location,
            district_id=self.district_id,
            shift=self.shift,
            skills=set(self.skills),
            equipment=set(self.equipment),
            max_jobs=self.max_jobs,
            transport_modes={self.transport},
            travel_mode=self.transport,
            travel_speed_kmh=self.travel_speed_kmh,
        )


class JobRule(StrictModel):
    bk_type: str = Field(min_length=1)
    hd_type: str | None = None  # An exact subtype overrides the wildcard BK rule.
    skill: Skill
    service_minutes: int | None = Field(default=None, ge=1, le=1440, strict=True)
    service_norm: ServiceNormCode | None = None
    priority: Priority
    required_transport: Transport | None
    required_equipment: set[Equipment] = Field(default_factory=set)
    window_semantics: Literal["start", "completion"] = "start"
    note: str = Field(min_length=1)

    @model_validator(mode="after")
    def resolve_service_duration(self) -> JobRule:
        if self.service_norm is not None:
            norm = SERVICE_NORMS[self.service_norm]
            if self.skill not in norm.allowed_skills:
                raise ValueError("Навык правила не соответствует выбранному нормативу")
            if self.service_minutes is not None and self.service_minutes != norm.service_minutes:
                raise ValueError(
                    f"Норматив {self.service_norm} задаёт {norm.service_minutes} мин на месте; "
                    "дорога рассчитывается отдельно. Для своей длительности уберите service_norm."
                )
            self.service_minutes = norm.service_minutes
        elif self.service_minutes is None:
            raise ValueError("Укажите service_minutes или service_norm")
        return self


class LocationReview(StrictModel):
    location: Location
    source: str = Field(min_length=1, max_length=1000)
    method: Literal["user_selected_candidate", "user_entered_coordinates"]
    csv_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ImportProfile(StrictModel):
    name: str = Field(min_length=1)
    notes: list[str] = Field(min_length=1)
    planning_time: AwareDatetime
    utc_offset_minutes: int = Field(default=180, ge=-720, le=840, strict=True)
    engineers: list[ImportEngineer] = Field(min_length=1, max_length=100)
    locations: dict[str, Location]
    district_mode: DistrictMode = DistrictMode.unrestricted
    address_districts: dict[str, DistrictId] = Field(default_factory=dict)
    district_source: Literal["profile", "csv"] = "profile"
    location_reviews: dict[str, LocationReview] = Field(default_factory=dict)
    job_rules: list[JobRule] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_profile_keys(self) -> ImportProfile:
        ids = [engineer.id for engineer in self.engineers]
        if len(ids) != len(set(ids)):
            raise ValueError("ID инженеров в профиле должны быть уникальны")
        addresses = [key(address) for address in self.locations]
        if "" in addresses or len(addresses) != len(set(addresses)):
            raise ValueError(
                "Адреса профиля пусты или повторяются после нормализации пробелов/регистра"
            )
        district_keys = [key(address) for address in self.address_districts]
        if "" in district_keys or len(district_keys) != len(set(district_keys)):
            raise ValueError("Пустые или повторяющиеся адреса в address_districts")
        if set(district_keys) - set(addresses):
            raise ValueError("Адрес из address_districts отсутствует в locations")
        if self.district_mode == DistrictMode.strict and any(e.district_id is None for e in self.engineers):
            raise ValueError("Для строгого режима у каждого инженера профиля нужен district_id")
        review_keys = [key(address) for address in self.location_reviews]
        if len(review_keys) != len(set(review_keys)):
            raise ValueError("Повторяющиеся адреса проверки координат")
        locations = {key(address): location for address, location in self.locations.items()}
        for address, review in self.location_reviews.items():
            location = locations.get(key(address))
            if location is None or (location.lat, location.lon) != (review.location.lat, review.location.lon):
                raise ValueError("Проверка координат не соответствует адресу профиля")
        rules = [
            (key(rule.bk_type), key(rule.hd_type) if rule.hd_type else None)
            for rule in self.job_rules
        ]
        if len(rules) != len(set(rules)):
            raise ValueError("Повторяющиеся правила BK/HD в профиле")
        if any(not note.strip() for note in self.notes):
            raise ValueError("Примечания профиля не должны быть пустыми")
        return self


class CsvSource(StrictModel):
    filename: str = Field(default="input.csv", min_length=1, max_length=255)
    csv_base64: str = Field(max_length=1_400_000)


class CsvImportInput(CsvSource):
    profile: ImportProfile | None = None
    history: CsvSource | None = None


class ImportIssue(BaseModel):
    severity: Literal["error", "warning"]
    code: str
    message: str
    row: int | None = None
    field: str | None = None


class ImportRow(BaseModel):
    row: int
    end_row: int
    kind: Literal["job", "office", "blank", "invalid"]
    cells: list[str]
    source_id: str | None = None
    address: str | None = None
    bk_type: str | None = None
    hd_type: str | None = None
    window: TimeWindow | None = None
    sla_deadline: AwareDatetime | None = None


class ImportSummary(BaseModel):
    total_rows: int
    job_rows: int
    office_rows: int
    blank_rows: int
    invalid_rows: int
    errors: int
    warnings: int


class CrewHistory(BaseModel):
    name: str
    rows: list[int]
    record_count: int
    unique_job_ids: int
    bk_types: list[str]
    hd_types: list[str]
    statuses: dict[str, int]
    observed_skills: list[Skill]
    unresolved_rows: list[int]


class HistoryReport(BaseModel):
    filename: str
    sha256: str
    headers: list[str]
    rows: list[ImportRow]
    summary: ImportSummary
    issues: list[ImportIssue]
    crews: list[CrewHistory]


class CsvImportResult(BaseModel):
    filename: str
    sha256: str
    encoding: str | None
    headers: list[str]
    rows: list[ImportRow]
    issues: list[ImportIssue]
    summary: ImportSummary
    profile_template: dict
    request: PlanRequest | None = None
    crew_history: list[CrewHistory] = Field(default_factory=list)
    history: HistoryReport | None = None
