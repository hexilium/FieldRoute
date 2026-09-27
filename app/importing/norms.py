"""Organizer service norms; road allowance is replaced by the routing model."""

from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Literal

ServiceNormCode = Literal["connection_basic", "tkd_emergency", "equipment_order", "local_repair"]
SOURCE_FILENAME = "Нормативы.xlsx"
SOURCE_SHA256 = "b82b3f1062be039835086c0471fd0247e0bf2de42d8c3e5cf9a59f4cf8c8482b"


@dataclass(frozen=True)
class ServiceNorm:
    code: ServiceNormCode
    title: str
    skill: str
    row: int
    travel_allowance_minutes: int
    technical_minutes: int
    documentation_minutes: int
    base_minutes: int

    @property
    def service_minutes(self) -> int:
        return self.technical_minutes + self.documentation_minutes

    @property
    def allowed_skills(self) -> tuple[str, ...]:
        # Existing profiles explicitly mapped equipment orders to connection.
        # Accept that choice without granting new qualifications to engineers.
        return (self.skill, "connection") if self.code == "equipment_order" else (self.skill,)

    def matches_snapshot(self, record: object) -> bool:
        current = self.snapshot()
        if record == current:
            return True
        # The source workbook and all durations are unchanged. Only the default
        # application skill mapping changed; old saved-plan evidence stays valid.
        return self.code == "equipment_order" and record == current | {"skill": "connection"}

    def snapshot(self) -> dict:
        value = asdict(self)
        value.pop("row")
        return value | {
            "service_minutes": self.service_minutes,
            "duration_basis": "technical_plus_documentation",
            "travel_policy": "routing_replaces_allowance",
            "source": {
                "filename": SOURCE_FILENAME, "sha256": SOURCE_SHA256,
                "sheet": "Лист1", "range": f"A{self.row}:E{self.row}",
                "base_formula": f"SUM(B{self.row}:D{self.row})",
            },
        }


# Labels and all four numeric columns are transcribed without reinterpretation.
# Skill codes and the mapping below belong to this application's import model.
SERVICE_NORMS = MappingProxyType({norm.code: norm for norm in (
    ServiceNorm("connection_basic", "Подключение клиентов Базовая", "connection", 2, 20, 60, 10, 90),
    ServiceNorm("tkd_emergency", "Аварий на ТКД", "emergency", 3, 20, 80, 0, 100),
    ServiceNorm("equipment_order", "Дозаказ оборудования", "additional_order", 4, 20, 10, 10, 40),
    ServiceNorm("local_repair", "Локальная заявка/ремонт у клиента", "local", 5, 20, 30, 0, 50),
)})


def suggested_norm(bk_type: str, hd_type: str) -> ServiceNorm | None:
    """Propose a mapping for a profile; the workbook itself has no BK/HD codes."""
    bk, hd = (" ".join(value.split()).casefold() for value in (bk_type, hd_type))
    if bk == "локальная заявка":
        return SERVICE_NORMS["local_repair"]
    if bk == "подключение":
        return SERVICE_NORMS["connection_basic"]
    if bk == "глобальная проблема" and hd == "авария":
        return SERVICE_NORMS["tkd_emergency"]
    if bk == "дозаказ" and hd in {
        "дозаказ оборудования", "заказ подключения/дозаказ оборудования",
    }:
        return SERVICE_NORMS["equipment_order"]
    return None


def norm_note(norm: ServiceNorm) -> str:
    return (
        f"Предложено сопоставление BK/HD с нормативом «{norm.title}» "
        f"из {SOURCE_FILENAME}, Лист1!A{norm.row}:E{norm.row}. "
        f"На месте: {norm.technical_minutes} мин работ + "
        f"{norm.documentation_minutes} мин документов = {norm.service_minutes} мин. "
        f"Вместо нормативных {norm.travel_allowance_minutes} мин дороги используется "
        "расчёт переезда. Проверьте применимость к подтипу заявки."
    )
