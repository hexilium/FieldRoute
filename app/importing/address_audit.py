"""Read-only, local address candidates for every job and office in a CSV report."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from app.config import Settings
from app.geodata import normalize
from app.importing.models import CsvImportResult, ImportIssue, ImportSummary, key
from app.services.geocoding import ATTRIBUTION, GeocodedPlace, GeocodingError
from app.services.local_geocoding import LocalGeocodingService

SCOPE = (
    "Показаны до пяти кандидатов из локального индекса для каждого адреса. "
    "Даже один кандидат требует проверки улицы, дома, корпуса и населённого пункта. "
    "Координаты профиля и план не изменяются. Вход в здание и привязка к дорожной "
    "сети не проверены; отсутствие результата не доказывает отсутствие адреса."
)
MOSCOW_PREFIX = re.compile(r"^(?:(?:город|г\.?)\s*)?москва(?:\s*,\s*|\s+)", re.IGNORECASE)


class AddressUse(BaseModel):
    row: int
    end_row: int
    kind: Literal["job", "office"]
    job_id: str | None
    address: str


class AddressReview(BaseModel):
    address: str
    uses: list[AddressUse]
    status: Literal["one_candidate", "multiple_candidates", "not_found"]
    queries: list[str]
    city_omitted: bool
    candidates: list[GeocodedPlace]


class AddressAuditSummary(BaseModel):
    addresses: int
    job_rows: int
    office_rows: int
    one_candidate: int
    multiple_candidates: int
    not_found: int
    city_omitted: int


class AddressAudit(BaseModel):
    filename: str
    sha256: str
    source_summary: ImportSummary
    source_issues: list[ImportIssue]
    index_metadata: dict[str, str]
    query_normalization_sha256: str
    summary: AddressAuditSummary
    addresses: list[AddressReview]
    scope: str = SCOPE
    attribution: str = ATTRIBUTION


async def audit_addresses(report: CsvImportResult, settings: Settings) -> AddressAudit:
    # This feature deliberately uses the local provider even if the interactive
    # single-address search was configured to use an external geocoder.
    return await asyncio.to_thread(_audit, report, LocalGeocodingService(settings))


def _audit(report: CsvImportResult, service: LocalGeocodingService) -> AddressAudit:
    import sqlite3

    try:
        with closing(service.connect()) as connection:
            connection.execute("BEGIN")
            has_metadata = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'",
            ).fetchone()
            allowed = {"schema_version", "source_sha256", "normalization_sha256", "built_at",
                       "address_count"}
            metadata = {row[0]: row[1] for row in connection.execute("SELECT key,value FROM metadata")
                        if row[0] in allowed} if has_metadata else {}
            reviews = _reviews(report, service, connection)
    except (OSError, sqlite3.Error) as exc:
        raise GeocodingError("Локальный индекс адресов недоступен. Подготовьте геоданные региона.") from exc

    counts = Counter(review.status for review in reviews)
    return AddressAudit(
        filename=report.filename, sha256=report.sha256, source_summary=report.summary,
        source_issues=report.issues, index_metadata=metadata,
        query_normalization_sha256=hashlib.sha256(Path(normalize.__file__).read_bytes()).hexdigest(),
        addresses=reviews,
        summary=AddressAuditSummary(
            addresses=len(reviews),
            job_rows=sum(use.kind == "job" for review in reviews for use in review.uses),
            office_rows=sum(use.kind == "office" for review in reviews for use in review.uses),
            one_candidate=counts["one_candidate"], multiple_candidates=counts["multiple_candidates"],
            not_found=counts["not_found"], city_omitted=sum(r.city_omitted for r in reviews),
        ),
    )


def _reviews(report: CsvImportResult, service: LocalGeocodingService, connection) -> list[AddressReview]:
    grouped: dict[str, list[AddressUse]] = {}
    for row in report.rows:
        if row.kind in {"job", "office"} and row.address:
            grouped.setdefault(key(row.address), []).append(AddressUse(
                row=row.row, end_row=row.end_row, kind=row.kind,
                job_id=row.source_id if row.kind == "job" else None, address=row.address,
            ))
    reviews = []
    for uses in grouped.values():
        address = uses[0].address
        queries = [address]
        candidates = service._search(address, None, None, connection=connection)
        city_omitted = False
        # Many OSM buildings lack addr:city. A relaxed search is only a visible
        # suggestion: never apply its coordinates or remove another municipality.
        without_city = MOSCOW_PREFIX.sub("", address, count=1).strip()
        if not candidates and without_city and without_city != address:
            queries.append(without_city)
            city_omitted = True
            candidates = service._search(without_city, None, None, connection=connection)
        reviews.append(AddressReview(
            address=address, uses=uses, queries=queries, city_omitted=city_omitted,
            candidates=candidates,
            status=("not_found" if not candidates else
                    "one_candidate" if len(candidates) == 1 else "multiple_candidates"),
        ))
    return reviews
