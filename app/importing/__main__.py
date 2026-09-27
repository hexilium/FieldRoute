"""Run with python -m app.importing input.csv --report report.json [--profile ...]."""

import argparse
import asyncio
import json
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings
from app.importing.address_audit import audit_addresses
from app.importing.csv_import import import_csv
from app.importing.models import ImportProfile
from app.services.geocoding import GeocodingError
from app.services.local_geocoding import geodata_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверка и подготовка CSV организатора")
    parser.add_argument("csv", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--template", type=Path)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--address-report", type=Path, help="Кандидаты адресов из локального индекса")
    parser.add_argument("--geodata-path", type=Path, help="Индекс для --address-report")
    args = parser.parse_args()
    if args.geodata_path and not args.address_report:
        parser.error("--geodata-path используется вместе с --address-report.")
    settings = Settings() if args.address_report else None
    if args.geodata_path:
        settings.geodata_path = str(args.geodata_path)
    inputs = {args.csv.resolve()}
    if args.profile:
        inputs.add(args.profile.resolve())
    if args.address_report:
        inputs.add(geodata_path(settings).resolve())
    outputs = [p.resolve() for p in (args.report, args.template, args.request, args.address_report) if p]
    if inputs.intersection(outputs) or len(set(outputs)) != len(outputs):
        parser.error("Выходные файлы должны отличаться друг от друга и от исходных файлов.")
    try:
        profile = (
            ImportProfile.model_validate_json(args.profile.read_text()) if args.profile else None
        )
        report = import_csv(args.csv.read_bytes(), args.csv.name, profile)
        addresses = asyncio.run(audit_addresses(report, settings)) if args.address_report else None
    except (OSError, ValidationError, GeocodingError) as exc:
        parser.error(str(exc))
    args.report.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    if args.template:
        args.template.write_text(
            json.dumps(report.profile_template, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.request and report.request:
        args.request.write_text(report.request.model_dump_json(indent=2), encoding="utf-8")
    if addresses:
        args.address_report.write_text(addresses.model_dump_json(indent=2), encoding="utf-8")
        summary = addresses.summary
        print(f"Адресов: {summary.addresses}; один кандидат: {summary.one_candidate}; "
              f"несколько: {summary.multiple_candidates}; не найдены: {summary.not_found}. "
              "Координаты не применены; кандидаты требуют проверки.")
    print(
        f"Строк: {report.summary.total_rows}; заявок: {report.summary.job_rows}; ошибок: {report.summary.errors}"
    )
    if args.request and report.request is None:
        print("Запрос не записан: исправьте ошибки CSV и заполните профиль.")
        return 1
    return 1 if report.summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
