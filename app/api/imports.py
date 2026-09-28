from base64 import b64decode, b64encode
from binascii import Error
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.importing.address_audit import AddressAudit, audit_addresses
from app.importing.csv_import import add_crews_to_template, import_csv
from app.importing.location_review import (
    LocationReviewInput,
    LocationReviewResult,
    reviewed_profile,
)
from app.importing.models import (
    PRIORITIES,
    SKILLS,
    TRANSPORTS,
    CsvImportInput,
    CsvImportResult,
    CsvSource,
    HistoryReport,
    ImportProfile,
)
from app.importing.norms import SERVICE_NORMS, norm_note
from app.services.geocoding import GeocodingError

router = APIRouter(prefix="/api/v1")


@router.post("/imports/addresses", response_model=AddressAudit)
async def inspect_addresses(
    payload: CsvSource, settings: Annotated[Settings, Depends(get_settings)],
) -> AddressAudit:
    try:
        data = b64decode(payload.csv_base64, validate=True)
    except (Error, ValueError):
        raise HTTPException(status_code=422, detail="csv_base64 должен содержать корректный Base64.") from None
    report = import_csv(data, payload.filename)
    if settings.roads_remote:
        raise HTTPException(503, "Массовая проверка адресов CSV требует локального индекса. "
                            "Во внешнем режиме используйте готовые координаты или ручной поиск адреса.")
    try:
        return await audit_addresses(report, settings)
    except GeocodingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/catalogs")
async def catalogs() -> dict:
    return {
        "skills": SKILLS, "transport": TRANSPORTS, "priority": PRIORITIES,
        "service_norms": [
            norm.snapshot() | {"allowed_skills": list(norm.allowed_skills)}
            for norm in SERVICE_NORMS.values()
        ],
    }


@router.post("/imports/csv", response_model=CsvImportResult)
async def inspect_csv(payload: CsvImportInput) -> CsvImportResult:
    try:
        data = b64decode(payload.csv_base64, validate=True)
    except (Error, ValueError):
        raise HTTPException(
            status_code=422, detail="csv_base64 должен содержать корректный Base64."
        ) from None
    report = import_csv(data, payload.filename, payload.profile)
    if payload.history:
        try:
            history_data = b64decode(payload.history.csv_base64, validate=True)
        except (Error, ValueError):
            raise HTTPException(
                status_code=422, detail="История должна содержать корректный Base64."
            ) from None
        history = import_csv(history_data, payload.history.filename)
        # Repeated IDs are observations in history, not colliding IDs of future jobs.
        for issue in history.issues:
            if issue.code == "duplicate_id":
                issue.severity = "warning"
                issue.message = issue.message.replace(
                    "Исправьте исходный CSV; строки не удалены.",
                    "Все записи истории сохранены; с синтетическими заявками ID не связывается.",
                )
        history.summary.errors = sum(i.severity == "error" for i in history.issues)
        history.summary.warnings = sum(i.severity == "warning" for i in history.issues)
        report.history = HistoryReport(
            filename=history.filename,
            sha256=history.sha256,
            headers=history.headers,
            rows=history.rows,
            summary=history.summary,
            issues=history.issues,
            crews=history.crew_history,
        )
        add_crews_to_template(report.profile_template, history.crew_history)
        report.profile_template["notes"].append(
            f"История бригад: {history.filename}; SHA-256: {history.sha256}."
        )
    return report


@router.post("/imports/locations", response_model=LocationReviewResult)
async def apply_locations(payload: LocationReviewInput) -> LocationReviewResult:
    source = CsvImportInput.model_validate(payload.model_dump(exclude={
        "selections", "source_sha256", "office_start_address",
    }))
    report = await inspect_csv(source)
    try:
        profile, issues = reviewed_profile(payload, report)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not issues:
        source.profile = ImportProfile.model_validate(profile)
        report = await inspect_csv(source)
    return LocationReviewResult(profile=profile, profile_issues=issues, report=report)


@router.get("/imports/example", response_model=CsvImportInput)
async def import_example(
    use_organizer_norms: bool = False,
    dataset: Literal["training", "realistic", "capabilities"] = "training",
) -> CsvImportInput:
    root = Path(__file__).resolve().parents[1] / "demo" / "data"
    if dataset != "training" and use_organizer_norms:
        raise HTTPException(status_code=422, detail="Этот переключатель нормативов доступен для учебного набора 12 / 36.")
    profile = ImportProfile.model_validate_json(
        (root / f"{dataset}_profile.json").read_text(encoding="utf-8")
    )
    if use_organizer_norms:
        data = profile.model_dump()
        data["name"] = "Учебный набор 12 / 36 с нормативами организатора"
        data["notes"] = [
            "Учебные адреса и координаты условные, не относятся к заявкам организатора.",
            ("Начало/Окончание — окно начала визита, UTC+03:00; смены, навыки, транспорт "
             "и приоритеты выбраны для демонстрации."),
            *[note for note in data["notes"] if "Haversine" in note],
            "Длительности работ взяты из Нормативы.xlsx; время дороги рассчитывается отдельно.",
        ]
        codes = {"local": "local_repair", "connection": "connection_basic", "emergency": "tkd_emergency"}
        for rule in data["job_rules"]:
            norm = SERVICE_NORMS[codes[rule["skill"]]]
            rule.update(service_norm=norm.code, service_minutes=norm.service_minutes, note=norm_note(norm))
        profile = ImportProfile.model_validate(data)
    return CsvImportInput(
        filename="training-with-norms.csv" if use_organizer_norms else f"{dataset}.csv",
        csv_base64=b64encode((root / f"{dataset}.csv").read_bytes()).decode("ascii"),
        profile=profile,
    )
