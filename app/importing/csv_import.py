"""Organizer CSV -> lossless row report -> explicitly enriched PlanRequest.

No network geocoding, invented coordinates, historical assignment locks or partial plans.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

from app.domain.models import Job, PlanRequest, TimeWindow
from app.importing.models import (
    PRIORITIES,
    CrewHistory,
    CsvImportResult,
    ImportIssue,
    ImportProfile,
    ImportRow,
    ImportSummary,
    key,
)
from app.importing.norms import SERVICE_NORMS, norm_note, suggested_norm

MAX_BYTES = 1_000_000
MAX_ROWS = 1000
REQUIRED_HEADERS = ("Заявка", "Тип заявки BK", "Тип заявки HD", "Начало", "Окончание", "Адрес")
KNOWN_HEADERS = {
    *REQUIRED_HEADERS,
    "Срок SLA",
    "Район",
    "Статус BK",
    "Бригада",
    "Подключение",
    "Гигабитное подключение",
}


def suggested_skill(bk: str, hd: str) -> str | None:
    if key(bk) == key("Локальная заявка"):
        return "local"
    if key(bk) == key("Подключение"):
        return "connection"
    if key(bk) == key("Дозаказ"):
        return "additional_order"
    if key(bk) == key("Глобальная проблема") and key(hd) == key("Авария"):
        return "emergency"
    return None


def summarize_crews(rows: list[ImportRow], headers: list[str]) -> list[CrewHistory]:
    grouped = defaultdict(list)
    for row in rows:
        record = dict(zip(headers, row.cells))
        name = record.get("Бригада", "").strip()
        if name and row.kind == "job":
            grouped[name].append((row, record))
    return [
        CrewHistory(
            name=name,
            rows=[row.row for row, _ in entries],
            record_count=len(entries),
            unique_job_ids=len({row.source_id for row, _ in entries}),
            bk_types=sorted({row.bk_type for row, _ in entries}),
            hd_types=sorted({row.hd_type for row, _ in entries}),
            statuses=dict(Counter(record.get("Статус BK", "").strip() for _, record in entries)),
            observed_skills=sorted(
                {
                    skill
                    for row, _ in entries
                    if (skill := suggested_skill(row.bk_type, row.hd_type))
                }
            ),
            unresolved_rows=[
                row.row for row, _ in entries if suggested_skill(row.bk_type, row.hd_type) is None
            ],
        )
        for name, entries in grouped.items()
    ]


def add_crews_to_template(template: dict, crews: list[CrewHistory]) -> None:
    if not crews:
        return
    origin = template["engineers"][0]["start_location"]
    template["engineers"] = [
        {
            "id": f"crew-{index + 1:02}",
            "district_id": None,
            "name": crew.name,
            "start_location": dict(origin),
            "shift": {"start": None, "end": None},
            "skills": crew.observed_skills,
            "equipment": [],
            "max_jobs": None,
            "transport": None,
        }
        for index, crew in enumerate(crews)
    ]
    template["notes"].append(
        "Список бригад и наблюдаемые навыки выведены из контрольной истории. "
        "Проверьте полноту навыков и неразобранные типы BK/HD; смены, транспорт, "
        "старты нужно заполнить отдельно; длительности предлагаются по нормативам, "
        "когда тип работы распознан."
    )


def profile_template(rows: list[ImportRow]) -> dict:
    jobs = [row for row in rows if row.kind == "job"]
    rules = {}
    locations = {}
    for row in jobs:
        locations.setdefault(row.address, {"lat": None, "lon": None, "label": row.address})
        # Preserve ambiguous subtype as its own unresolved rule.
        hd = row.hd_type if key(row.bk_type) in {"глобальная проблема", "дозаказ"} else None
        norm = suggested_norm(row.bk_type, row.hd_type)
        rules.setdefault(
            (key(row.bk_type), key(hd or "")),
            {
                "bk_type": row.bk_type,
                "hd_type": hd,
                "skill": suggested_skill(row.bk_type, row.hd_type),
                "service_minutes": norm.service_minutes if norm else None,
                "service_norm": norm.code if norm else None,
                "priority": None,
                "required_transport": None,
                "required_equipment": [],
                "note": norm_note(norm) if norm else "",
            },
        )
    office = next((row.address for row in rows if row.kind == "office"), "Старт инженера")
    return {
        "name": "",
        "notes": [
            "Укажите источники координат и допущения о сменах и работах.",
            ("Нормативы.xlsx: время на месте включает технические работы и документы; "
            "дорога рассчитывается отдельно. Сопоставление BK/HD предложено импортом. "
            "Проверьте базовое подключение для FMC/FTTB/Гбит: отдельных коэффициентов в книге нет."),
            ("Подключения и дозаказы — отдельные навыки connection и additional_order. "
             "Квалификация аварий emergency задаётся отдельно; навыки не следуют из оборудования."),
            ("Заполните equipment инженеров и required_equipment правил. Это наличие "
             "оборудования на всю смену, без учёта количества, расходования и пополнения. "
             "Пустой список требований означает, что оборудование для работы не проверяется. "
             "max_jobs = null означает отсутствие отдельного лимита заявок; смена ограничена временем."),
        ],
        "district_mode": "unrestricted",
        "district_source": "profile",
        "address_districts": {},
        "planning_time": None,
        "utc_offset_minutes": 180,
        "engineers": [
            {
                "id": "engineer-1",
                "district_id": None,
                "name": "",
                "start_location": {
                    "lat": None,
                    "lon": None,
                    "label": office,
                },
                "shift": {"start": None, "end": None},
                "skills": [],
                "equipment": [],
                "max_jobs": None,
                "transport": None,
            }
        ],
        "locations": locations,
        "job_rules": list(rules.values()),
    }


def import_csv(data: bytes, filename: str, profile: ImportProfile | None = None) -> CsvImportResult:
    issues: list[ImportIssue] = []
    rows: list[ImportRow] = []
    headers: list[str] = []
    encoding = None
    plan = None

    def issue(code, message, row=None, field=None, severity="error"):
        issues.append(
            ImportIssue(code=code, message=message, row=row, field=field, severity=severity)
        )

    def result():
        counts = {
            kind: sum(row.kind == kind for row in rows)
            for kind in ("job", "office", "blank", "invalid")
        }
        template = profile_template(rows)
        crews = summarize_crews(rows, headers)
        add_crews_to_template(template, crews)
        if profile:
            template["utc_offset_minutes"] = profile.utc_offset_minutes
        return CsvImportResult(
            filename=filename,
            sha256=hashlib.sha256(data).hexdigest(),
            encoding=encoding,
            headers=headers,
            rows=rows,
            issues=issues,
            request=plan,
            profile_template=template,
            crew_history=crews,
            summary=ImportSummary(
                total_rows=len(rows),
                job_rows=counts["job"],
                office_rows=counts["office"],
                blank_rows=counts["blank"],
                invalid_rows=counts["invalid"],
                errors=sum(i.severity == "error" for i in issues),
                warnings=sum(i.severity == "warning" for i in issues),
            ),
        )

    if len(data) > MAX_BYTES:
        issue("file_too_large", "Размер CSV превышает 1 МБ.")
        return result()
    try:
        text = data.decode("utf-8-sig")
        encoding = "utf-8"
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1251")
            encoding = "windows-1251"
        except UnicodeDecodeError:
            issue("invalid_encoding", "Ожидается UTF-8 или Windows-1251 без повреждённых байтов.")
            return result()
    if "\x00" in text:
        issue("binary_file", "Файл содержит нулевые байты. Выберите CSV в UTF-8 или Windows-1251.")
        return result()
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=";", strict=True)
    try:
        raw_headers = next(reader, [])
        headers = [value.strip() for value in raw_headers]
        if not headers:
            issue("empty_file", "CSV пуст: отсутствует строка заголовков.")
            return result()
        if len(headers) != len(set(headers)) or any(not header for header in headers):
            issue("duplicate_headers", "Заголовки пусты или повторяются.")
        missing = set(REQUIRED_HEADERS) - set(headers)
        if missing:
            issue(
                "missing_headers",
                "Ожидается CSV с разделителем «;». Нет колонок: " + ", ".join(sorted(missing)),
            )
        if any(i.severity == "error" for i in issues):
            return result()
        unknown = set(headers) - KNOWN_HEADERS
        if unknown:
            issue(
                "extra_headers",
                "Дополнительные колонки сохранены в исходных строках: "
                + ", ".join(sorted(unknown)),
                severity="warning",
            )
        ids = defaultdict(list)
        tz = timezone(timedelta(minutes=profile.utc_offset_minutes if profile else 180))
        while True:
            start_line = reader.line_num + 1
            cells = next(reader, None)
            if cells is None:
                break
            if len(rows) >= MAX_ROWS:
                issue(
                    "too_many_rows",
                    "Допускается не более 1000 строк данных. Отчёт неполный; план не создан.",
                    start_line,
                )
                break
            row = ImportRow(row=start_line, end_row=reader.line_num, kind="invalid", cells=cells)
            rows.append(row)
            if not any(cell.strip() for cell in cells):
                row.kind = "blank"
                continue
            if len(cells) != len(headers):
                issue(
                    "column_count",
                    f"Ожидалось {len(headers)} колонок, получено {len(cells)}.",
                    start_line,
                )
                continue
            record = dict(zip(headers, (cell.strip() for cell in cells)))
            source_id = record["Заявка"]
            if key(source_id) == key("Адрес офиса"):
                row.kind, row.address = "office", record["Тип заявки BK"]
                if not row.address or any(
                    value.strip()
                    for index, value in enumerate(cells)
                    if headers[index] not in {"Заявка", "Тип заявки BK"}
                ):
                    issue(
                        "invalid_office",
                        "Строка офиса должна содержать только метку и адрес во второй колонке.",
                        start_line,
                    )
                continue
            row.source_id = source_id
            row.address, row.bk_type, row.hd_type = (
                record["Адрес"],
                record["Тип заявки BK"],
                record["Тип заявки HD"],
            )
            if not source_id:
                issue("missing_id", "У заявки отсутствует ID.", start_line, "Заявка")
            else:
                ids[source_id].append(start_line)
            for field in ("Адрес", "Тип заявки BK", "Тип заявки HD"):
                if not record[field]:
                    issue("missing_value", f"Пустое поле «{field}».", start_line, field)
            try:
                start = datetime.strptime(record["Начало"], "%d.%m.%Y %H:%M").replace(tzinfo=tz)
                end = datetime.strptime(record["Окончание"], "%d.%m.%Y %H:%M").replace(tzinfo=tz)
                row.window = TimeWindow(start=start, end=end)
            except ValueError:
                issue(
                    "invalid_window",
                    "Ожидается ДД.ММ.ГГГГ ЧЧ:ММ; окончание должно быть позже начала. Для ночного окна укажите следующую дату.",
                    start_line,
                    "Начало/Окончание",
                )
            sla_valid = True
            if sla := record.get("Срок SLA", ""):
                try:
                    row.sla_deadline = datetime.strptime(sla, "%d.%m.%Y %H:%M").replace(
                        tzinfo=tz
                    )
                except ValueError:
                    sla_valid = False
                    issue(
                        "invalid_sla_deadline",
                        "Срок SLA должен иметь формат ДД.ММ.ГГГГ ЧЧ:ММ.",
                        start_line,
                        "Срок SLA",
                    )
            if (
                source_id
                and row.address
                and row.bk_type
                and row.hd_type
                and row.window
                and sla_valid
            ):
                row.kind = "job"
                if suggested_skill(row.bk_type, row.hd_type) is None and profile is None:
                    issue(
                        "unmapped_skill",
                        "Тип BK/HD требует явного решения о навыке в профиле.",
                        start_line,
                        "Тип заявки BK/HD",
                        "warning",
                    )
                if profile is None and suggested_norm(row.bk_type, row.hd_type) is None:
                    issue(
                        "unmapped_service_norm",
                        "Для этого BK/HD нет однозначного норматива. Укажите длительность "
                        "или выберите применимый норматив в профиле.",
                        start_line, "Тип заявки BK/HD", "warning",
                    )
        for source_id, line_numbers in ids.items():
            if len(line_numbers) > 1:
                for line in line_numbers:
                    issue(
                        "duplicate_id",
                        f"ID «{source_id}» повторяется в строках {', '.join(map(str, line_numbers))}. Исправьте исходный CSV; строки не удалены.",
                        line,
                        "Заявка",
                    )
    except csv.Error as exc:
        issue(
            "invalid_csv",
            f"Нарушена структура CSV: {exc}. Отчёт неполный; план не создан.",
            reader.line_num,
        )

    if not any(row.kind == "job" for row in rows):
        issue("no_jobs", "Нет корректных строк заявок.")
    if sum(row.kind == "office" for row in rows) > 1:
        issue(
            "multiple_offices",
            "Найдено несколько строк офиса; их адреса сохранены, старты задаются в профиле.",
            severity="warning",
        )
    offset = profile.utc_offset_minutes if profile else 180
    issue(
        "window_assumption",
        f"«Начало/Окончание» трактуются как окно начала визита; смещение от UTC: {offset:+d} минут.",
        severity="warning",
    )
    if {"Статус BK", "Бригада"} & set(headers):
        issue(
            "historical_reference",
            "Исторические статусы и бригады сохранены только как справочные поля. Все импортируемые заявки планируются заново; контрольное распределение не является алгоритмическим baseline.",
            severity="warning",
        )
    if profile is None:
        issue(
            "profile_required",
            "Для расчёта нужен профиль: координаты, инженеры, длительности, навыки, приоритеты и транспорт. Скачайте шаблон.",
            severity="warning",
        )
    elif not any(i.severity == "error" for i in issues):
        jobs = []
        locations = {key(address): location for address, location in profile.locations.items()}
        address_districts = {key(address): district for address, district in profile.address_districts.items()}
        location_reviews = {key(address): review for address, review in profile.location_reviews.items()}
        rules = {
            (key(r.bk_type), key(r.hd_type) if r.hd_type else None): r for r in profile.job_rules
        }
        for row in rows:
            if row.kind != "job":
                continue
            rule = rules.get((key(row.bk_type), key(row.hd_type))) or rules.get(
                (key(row.bk_type), None)
            )
            location = locations.get(key(row.address))
            if rule is None:
                issue("missing_rule", f"Нет правила для «{row.bk_type} / {row.hd_type}».", row.row)
            if location is None:
                issue("missing_location", "Нет координат адреса в профиле.", row.row, "Адрес")
            district_id = address_districts.get(key(row.address))
            if profile.district_source == "csv":
                csv_district = dict(zip(headers, row.cells)).get("Район", "").strip()
                if not csv_district or len(csv_district) > 120:
                    issue("invalid_csv_district", "Заполните колонку Район (1–120 символов).", row.row, "Район")
                    continue
                if district_id is not None and district_id != csv_district:
                    issue("district_conflict", "Район в CSV не совпадает с address_districts профиля.", row.row, "Район")
                    continue
                district_id = csv_district
            if profile.district_mode == "strict" and district_id is None:
                issue("missing_district", "Укажите район адреса в address_districts профиля.", row.row, "Адрес")
            if rule is None or location is None or (profile.district_mode == "strict" and district_id is None):
                continue
            if key(row.bk_type) == "дозаказ" and rule.skill == "connection":
                issue(
                    "legacy_order_skill",
                    "В профиле дозаказ требует навык connection. Это прежнее объединённое "
                    "сопоставление; оно сохранено. Для отдельной квалификации укажите "
                    "additional_order в правиле и у соответствующих инженеров.",
                    row.row,
                    severity="warning",
                )
            jobs.append(
                Job(
                    id=row.source_id,
                    district_id=district_id,
                    title=f"{row.hd_type} · {row.source_id}",
                    location=location.model_copy(update={"label": row.address}),
                    service_minutes=rule.service_minutes,
                    window_semantics=rule.window_semantics,
                    time_windows=[row.window],
                    required_skills={rule.skill},
                    required_equipment=set(rule.required_equipment),
                    priority=PRIORITIES[rule.priority],
                    required_transport=rule.required_transport,
                    sla_deadline=row.sla_deadline,
                    metadata={
                        "source": {
                            "filename": filename,
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "row": row.row,
                            "end_row": row.end_row,
                            "id": row.source_id,
                            "fields": dict(zip(headers, row.cells)),
                        },
                        "enrichment": {
                            **({"location_review": location_reviews[key(row.address)].model_dump(mode="json")}
                               if key(row.address) in location_reviews else {}),
                            "profile": profile.name,
                            "notes": profile.notes,
                            # Keep old snapshots byte-equivalent when the optional
                            # equipment constraint adds no requirement.
                            "rule": rule.model_dump(exclude=(
                                {"required_equipment"} if not rule.required_equipment else set()
                            )),
                            "utc_offset_minutes": offset,
                            **({"service_norm": (
                                SERVICE_NORMS[rule.service_norm].snapshot() | {"skill": rule.skill}
                            )}
                               if rule.service_norm else {}),
                        },
                    },
                )
            )
        if not any(i.severity == "error" for i in issues):
            plan = PlanRequest(
                planning_time=profile.planning_time,
                district_mode=profile.district_mode,
                engineers=[engineer.to_engineer() for engineer in profile.engineers],
                jobs=jobs,
            )
    return result()
