"""Apply explicit user coordinate choices to a copy of an import profile."""

from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from app.domain.models import Location
from app.importing.models import CsvImportInput, CsvImportResult, ImportProfile, StrictModel, key


class LocationSelection(StrictModel):
    address: str = Field(min_length=1, max_length=2000)
    location: Location
    source: str = Field(min_length=1, max_length=1000)
    method: Literal["user_selected_candidate", "user_entered_coordinates"]


class LocationReviewInput(CsvImportInput):
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selections: list[LocationSelection] = Field(min_length=1, max_length=1000)
    office_start_address: str | None = None


class LocationReviewResult(BaseModel):
    profile: dict
    profile_issues: list[str]
    report: CsvImportResult


def profile_issue(error: dict) -> str:
    labels = {
        "name": "название", "planning_time": "время начала расчёта",
        "utc_offset_minutes": "часовой пояс", "notes": "примечания",
        "engineers": "инженеры", "job_rules": "правила работ",
        "start_location": "старт", "lat": "широта", "lon": "долгота",
        "shift": "смена", "start": "начало", "end": "окончание",
        "skills": "навыки", "transport": "транспорт", "priority": "приоритет",
        "skill": "навык", "service_minutes": "минуты работы",
        "service_norm": "норматив", "note": "обоснование", "locations": "адреса",
    }
    path = " → ".join(f"№{part + 1}" if isinstance(part, int) else labels.get(part, part)
                       for part in error["loc"])
    message = error["msg"].removeprefix("Value error, ") if error["type"] == "value_error" else {
        "datetime_type": "укажите дату и время с часовым поясом",
        "datetime_parsing": "укажите дату и время с часовым поясом",
        "timezone_aware": "укажите часовой пояс даты и времени",
        "string_too_short": "заполните поле", "too_short": "выберите хотя бы одно значение",
        "float_type": "укажите число", "int_type": "укажите целое число",
        "literal_error": "выберите значение", "missing": "заполните поле",
    }.get(error["type"], "проверьте значение")
    return f"{path or 'Профиль'}: {message}"


def reviewed_profile(payload: LocationReviewInput, report: CsvImportResult) -> tuple[dict, list[str]]:
    if payload.source_sha256 != report.sha256:
        raise ValueError("CSV изменился после проверки адресов. Проверьте файл заново.")
    source_addresses = {
        key(row.address): row.address for row in reversed(report.rows)
        if row.kind in {"job", "office"} and row.address
    }
    selections = {key(item.address): item for item in payload.selections}
    if len(selections) != len(payload.selections):
        raise ValueError("Один адрес выбран несколько раз")
    if selections.keys() - source_addresses.keys():
        raise ValueError("Выбранный адрес отсутствует среди заявок и офисов CSV")
    if payload.office_start_address is not None:
        offices = {key(row.address) for row in report.rows if row.kind == "office" and row.address}
        if key(payload.office_start_address) not in offices & selections.keys():
            raise ValueError("Для старта инженеров выберите проверенные координаты офиса из CSV")

    profile = (payload.profile.model_dump(mode="json") if payload.profile
               else deepcopy(report.profile_template))
    # Templates can contain case/space variants of an address; the importer uses
    # the same key for all such rows. Preserve the first spelling, never merge streets.
    canonical = {}
    locations = {}
    for address, location in profile["locations"].items():
        if key(address) not in canonical:
            canonical[key(address)] = address
            locations[address] = location
    reviews = profile.setdefault("location_reviews", {})
    review_keys = {key(address): address for address in reviews}
    for address_key, selection in selections.items():
        address = canonical.get(address_key, source_addresses[address_key])
        location = selection.location.model_dump(mode="json")
        locations[address] = location
        reviews.pop(review_keys.get(address_key, address), None)
        reviews[address] = {
            "location": location, "source": selection.source, "method": selection.method,
            "csv_sha256": report.sha256,
        }
    profile["locations"] = locations
    if payload.office_start_address is not None:
        office = selections[key(payload.office_start_address)]
        for engineer in profile["engineers"]:
            engineer["start_location"] = office.location.model_dump(mode="json")
        note = (f"По выбору пользователя все инженеры стартуют из офиса: {office.address}; "
                f"{office.location.lat}, {office.location.lon}. Источник: {office.source}. "
                f"CSV SHA-256: {report.sha256}.")
        if note not in profile["notes"]:
            profile["notes"].append(note)
    try:
        ImportProfile.model_validate(profile)
    except ValidationError as exc:
        return profile, [profile_issue(error) for error in exc.errors(include_input=False)]
    return profile, []
