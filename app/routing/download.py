"""Download the Moscow OSM extract once, with atomic publication and integrity checks."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from pathlib import Path

SOURCE = "https://download.openstreetmap.fr/extracts/russia/central_federal_district/moscow.osm.pbf"


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(directory: Path, source: str = SOURCE) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target, manifest = directory / "moscow.osm.pbf", directory / "download.json"
    if target.exists() and manifest.exists():
        saved = json.loads(manifest.read_text())
        if saved.get("source") == source and saved.get("sha256") == digest(target):
            print("Карта Москвы уже загружена и проверена.", flush=True)
            return target
    partial = target.with_suffix(".pbf.partial")
    # The checksum and file can change during the upstream daily update. A retry
    # obtains both again; incomplete downloads never replace the previous file.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(source + ".md5", timeout=60) as response:
                expected = response.read(1024).decode().split()[0].lower()
            if not re.fullmatch(r"[0-9a-f]{32}", expected):
                raise ValueError("Источник не вернул контрольную сумму карты")
            md5 = hashlib.md5(usedforsecurity=False)
            count, report = 0, 0
            print("Скачивание дорожных данных Москвы…", flush=True)
            with urllib.request.urlopen(source, timeout=120) as response, partial.open("wb") as out:
                length = int(response.headers.get("Content-Length", "0"))
                while block := response.read(1024 * 1024):
                    out.write(block)
                    md5.update(block)
                    count += len(block)
                    if count - report >= 10 * 1024 * 1024:
                        print(f"Загружено {count // (1024 * 1024)} МБ", flush=True)
                        report = count
            if not count or (length and count != length) or md5.hexdigest() != expected:
                raise ValueError("Размер или контрольная сумма карты не совпадают")
            info = {"source": source, "sha256": digest(partial), "bytes": count}
            partial.replace(target)
            temporary = manifest.with_suffix(".tmp")
            temporary.write_text(json.dumps(info, indent=2) + "\n")
            temporary.replace(manifest)
            print("Карта Москвы сохранена. Подготовка дорожных графов…", flush=True)
            return target
        except (OSError, ValueError):
            partial.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2)
    raise AssertionError("unreachable")


if __name__ == "__main__":
    download(Path(os.environ.get("ROAD_DATA_DIR", "/data")))
