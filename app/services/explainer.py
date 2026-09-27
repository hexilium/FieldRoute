from __future__ import annotations

from datetime import datetime

from app.domain.models import Engineer, Job
from app.importing.norms import SERVICE_NORMS

SKILL_NAMES = {
    "local": "локальные работы",
    "connection": "подключение",
    "additional_order": "дозаказы",
    "emergency": "аварийные работы",
    "fiber": "оптика",
    "router": "маршрутизаторы",
    "voice": "телефония",
}
TRANSPORT_NAMES = {
    "car": "автомобиль",
    "walk": "пешком",
    "bicycle": "велосипед",
    "public_transport": "общественный транспорт",
}


def names(values: set[str], labels: dict[str, str] | None = None) -> str:
    return ", ".join((labels or {}).get(value, value) for value in sorted(values))


def at(value: datetime) -> str:
    return value.strftime("%d.%m %H:%M:%S %z")


def minutes_text(value: float) -> str:
    return "менее 0,1" if 0 < value < 0.1 else f"{value:.1f}"


def service_norm_explanation(job: Job) -> str | None:
    enrichment = job.metadata.get("enrichment")
    record = enrichment.get("service_norm") if isinstance(enrichment, dict) else None
    code = record.get("code") if isinstance(record, dict) else None
    norm = SERVICE_NORMS.get(code) if isinstance(code, str) else None
    if norm is None or not norm.matches_snapshot(record):
        return None
    text = (
        f"норматив «{norm.title}»: {norm.technical_minutes} мин работ + "
        f"{norm.documentation_minutes} мин документов = {norm.service_minutes} мин на месте; "
        "дорога учитывается отдельным переездом"
    )
    if job.service_minutes != norm.service_minutes:
        text += f"; в текущем запросе длительность изменена на {job.service_minutes} мин"
    if job.status == "in_progress":
        text += "; выполняемая работа сохраняет интервал предыдущего плана"
    return text


def assignment_explanation(
    job: Job,
    engineer: Engineer,
    travel_minutes: float,
    *,
    arrival: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[str]:
    parts: list[str] = []
    if job.required_skills:
        parts.append("требуемые навыки есть у инженера: " + names(job.required_skills, SKILL_NAMES))
    if job.required_equipment:
        parts.append("доступно необходимое оборудование: " + names(job.required_equipment))
    if job.required_transport:
        parts.append(
            "доступен требуемый транспорт: "
            + TRANSPORT_NAMES.get(job.required_transport, job.required_transport)
        )
    mode = TRANSPORT_NAMES[engineer.travel_mode]
    parts.append(f"переезд от предыдущей точки около {travel_minutes:.0f} мин ({mode})")
    if norm_text := service_norm_explanation(job):
        parts.append(norm_text)
    if job.priority >= 80:
        parts.append("заявка имеет высокий приоритет")
    if job.time_windows:
        parts.append(
            "работа целиком помещается во временное окно"
            if job.window_semantics == "completion"
            else "начало работы попадает во временное окно"
        )
    if arrival is not None and start is not None and end is not None:
        parts.append(f"прибытие {at(arrival)}; начало {at(start)}; окончание {at(end)}")
        if start > arrival:
            parts.append(
                f"ожидание до начала работ: {minutes_text((start - arrival).total_seconds() / 60)} мин"
            )
        matched = next(
            (
                w
                for w in job.time_windows
                if w.start <= start <= w.end and (job.window_semantics == "start" or end <= w.end)
            ),
            None,
        )
        if matched:
            boundary = end if job.window_semantics == "completion" else start
            parts.append(
                f"окно {at(matched.start)} — {at(matched.end)}; "
                f"запас до его конца {minutes_text((matched.end - boundary).total_seconds() / 60)} мин"
            )
        parts.append(
            f"смена {at(engineer.shift.start)} — {at(engineer.shift.end)}; "
            f"запас после этой работы {minutes_text((engineer.shift.end - end).total_seconds() / 60)} мин"
        )
        if job.sla_deadline:
            late = max(0, (start - job.sla_deadline).total_seconds() / 60)
            parts.append(
                f"мягкий срок начала {at(job.sla_deadline)}: "
                + (f"опоздание {minutes_text(late)} мин" if late else "соблюдён")
            )
    return parts
