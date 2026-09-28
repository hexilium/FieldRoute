"""Build a self-contained SQLite address/map index from an OSM extract.

Only this preparation command requires the optional ``osmium`` dependency.
Serving the resulting index needs Python's standard SQLite module only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from app.geodata.normalize import normalize_address

SCHEMA_VERSION = "1"
SCHEMA = """
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE addresses (
    id INTEGER PRIMARY KEY, lat REAL NOT NULL, lon REAL NOT NULL,
    label TEXT NOT NULL, street TEXT NOT NULL, housenumber TEXT NOT NULL,
    city TEXT NOT NULL, search_text TEXT NOT NULL
);
CREATE VIRTUAL TABLE addresses_fts USING fts5(
    search_text, content='addresses', content_rowid='id'
);
CREATE VIRTUAL TABLE addresses_rtree USING rtree(id,min_lon,max_lon,min_lat,max_lat);
CREATE TABLE features (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, min_zoom INTEGER NOT NULL,
    geometry TEXT NOT NULL, name TEXT NOT NULL
);
CREATE VIRTUAL TABLE features_rtree USING rtree(id,min_lon,max_lon,min_lat,max_lat);
CREATE INDEX features_zoom ON features(min_zoom);
"""


def _normalization_fingerprint() -> str:
    """Invalidate stored search terms when their normalization rules change."""
    return hashlib.sha256(Path(__file__).with_name("normalize.py").read_bytes()).hexdigest()


def _source_metadata(source: Path) -> dict[str, str]:
    stat = source.stat()
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_path": str(source.resolve()),
        "source_size": str(stat.st_size),
        "source_mtime_ns": str(stat.st_mtime_ns),
        "source_sha256": digest.hexdigest(),
        "normalization_sha256": _normalization_fingerprint(),
    }


def _existing_metadata(output: Path, source: dict[str, str]) -> dict[str, str] | None:
    if not output.is_file():
        return None
    try:
        with closing(sqlite3.connect(f"{output.resolve().as_uri()}?mode=ro", uri=True)) as connection:
            metadata = dict(connection.execute("SELECT key,value FROM metadata"))
            # The path can differ between host and container for the same source file.
            keys = ("schema_version", "source_size", "source_mtime_ns", "source_sha256",
                    "normalization_sha256")
            if any(metadata.get(key) != source[key] for key in keys):
                return None
            for table in ("addresses", "addresses_fts", "addresses_rtree", "features",
                          "features_rtree"):
                connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            return metadata
    except sqlite3.Error:
        return None


def _valid_point(lon: float, lat: float) -> bool:
    return math.isfinite(lon) and math.isfinite(lat) and -180 <= lon <= 180 and -90 <= lat <= 90


def _centroid(points: list[list[float]]) -> tuple[float, float]:
    """Polygon centroid, falling back to a vertex mean for lines/degenerate rings."""
    if len(points) >= 4 and points[0] == points[-1]:
        # Translate to the first vertex to avoid precision loss for small buildings.
        x0, y0 = points[0]
        area = cx = cy = 0.0
        for first, second in pairwise(points):
            x1, y1, x2, y2 = first[0] - x0, first[1] - y0, second[0] - x0, second[1] - y0
            cross = x1 * y2 - x2 * y1
            area += cross
            cx += (x1 + x2) * cross
            cy += (y1 + y2) * cross
        if abs(area) > 1e-18:
            return x0 + cx / (3 * area), y0 + cy / (3 * area)
        points = points[:-1]
    return sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)


def _road_zoom(highway: str) -> int | None:
    if highway in {"motorway", "motorway_link", "trunk", "trunk_link", "primary",
                   "primary_link"}:
        return 9
    if highway in {"secondary", "secondary_link"}:
        return 11
    if highway in {"tertiary", "tertiary_link"}:
        return 12
    if highway in {"residential", "living_street", "unclassified", "road"}:
        return 14
    if highway in {"service", "pedestrian", "footway", "path", "cycleway", "steps",
                   "track", "bridleway"}:
        return 16
    return None


class _IndexWriter:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.address_count = self.feature_count = 0
        self.bounds: list[float] | None = None
        self.address_batch: list[tuple] = []
        self.address_bounds_batch: list[tuple] = []
        self.feature_batch: list[tuple] = []
        self.feature_bounds_batch: list[tuple] = []

    def include_bounds(self, bounds: list[float]) -> None:
        if self.bounds is None:
            self.bounds = bounds[:]
        else:
            self.bounds = [min(self.bounds[0], bounds[0]), min(self.bounds[1], bounds[1]),
                           max(self.bounds[2], bounds[2]), max(self.bounds[3], bounds[3])]

    def address(self, tags: dict[str, str], lon: float, lat: float) -> None:
        street = (tags.get("addr:street") or tags.get("addr:place") or "").strip()
        number = tags.get("addr:housenumber", "").strip()
        if not street or not number or not _valid_point(lon, lat):
            return
        city = (tags.get("addr:city") or tags.get("addr:town") or tags.get("addr:village")
                or tags.get("addr:hamlet") or "").strip()
        label = ", ".join(part for part in (city, street, number) if part)
        self.address_count += 1
        lat, lon = round(lat, 7), round(lon, 7)
        self.address_batch.append((self.address_count, lat, lon, label, street, number, city,
                                   normalize_address(label)))
        self.address_bounds_batch.append((self.address_count, lon, lon, lat, lat))
        self.include_bounds([lon, lat, lon, lat])
        if len(self.address_batch) >= 1000:
            self.flush_addresses()

    def feature(self, kind: str, min_zoom: int, geometry: dict, name: str = "") -> None:
        if geometry["type"] == "LineString":
            points = geometry["coordinates"]
        else:
            points = [point for polygon in geometry["coordinates"]
                      for ring in polygon for point in ring]
        if not points or any(not _valid_point(*point) for point in points):
            return
        bounds = [min(p[0] for p in points), min(p[1] for p in points),
                  max(p[0] for p in points), max(p[1] for p in points)]
        self.feature_count += 1
        self.feature_batch.append((self.feature_count, kind, min_zoom,
                                   json.dumps(geometry, ensure_ascii=False, separators=(",", ":"),
                                              allow_nan=False), name))
        self.feature_bounds_batch.append((self.feature_count, bounds[0], bounds[2],
                                          bounds[1], bounds[3]))
        self.include_bounds(bounds)
        if len(self.feature_batch) >= 1000:
            self.flush_features()

    def flush_addresses(self) -> None:
        self.connection.executemany("INSERT INTO addresses VALUES (?,?,?,?,?,?,?,?)",
                                    self.address_batch)
        self.connection.executemany("INSERT INTO addresses_rtree VALUES (?,?,?,?,?)",
                                    self.address_bounds_batch)
        self.address_batch.clear()
        self.address_bounds_batch.clear()

    def flush_features(self) -> None:
        self.connection.executemany("INSERT INTO features VALUES (?,?,?,?,?)", self.feature_batch)
        self.connection.executemany("INSERT INTO features_rtree VALUES (?,?,?,?,?)",
                                    self.feature_bounds_batch)
        self.feature_batch.clear()
        self.feature_bounds_batch.clear()


def _import_osm(source: Path, writer: _IndexWriter, location_index: str) -> None:
    try:
        import osmium
    except ImportError as exc:
        raise RuntimeError(
            f"Не удалось загрузить osmium: {exc}. "
            "Для Docker пересоберите образ geodata-prepare; для запуска без Docker "
            "установите пакет .[geodata] и системную библиотеку libexpat1."
        ) from exc

    factory = osmium.geom.GeoJSONFactory()

    class Handler(osmium.SimpleHandler):
        def node(self, node):
            if node.tags.get("addr:housenumber") and node.location.valid():
                writer.address(dict(node.tags), node.location.lon, node.location.lat)

        def way(self, way):
            tags = dict(way.tags)
            zoom = _road_zoom(tags.get("highway", ""))
            has_address = bool(tags.get("addr:housenumber"))
            if zoom is None and not has_address:
                return
            if any(not node.location.valid() for node in way.nodes):
                return
            points = [[node.lon, node.lat] for node in way.nodes]
            if not points or any(not _valid_point(*point) for point in points):
                return
            if has_address:
                writer.address(tags, *_centroid(points))
            if zoom is not None and len(points) >= 2:
                writer.feature("road", zoom, {"type": "LineString", "coordinates": points},
                               tags.get("name", ""))

        def area(self, area):
            tags = dict(area.tags)
            building = tags.get("building") not in (None, "no")
            water = (tags.get("natural") == "water" or tags.get("waterway") == "riverbank"
                     or tags.get("landuse") in {"reservoir", "basin"})
            has_address = not area.from_way() and bool(tags.get("addr:housenumber"))
            if not (building or water or has_address):
                return
            try:
                geometry = json.loads(factory.create_multipolygon(area))
            except (RuntimeError, ValueError):
                return
            polygons = geometry.get("coordinates", [])
            if has_address and polygons and polygons[0] and polygons[0][0]:
                writer.address(tags, *_centroid(polygons[0][0]))
            if building or water:
                writer.feature("building" if building else "water", 16 if building else 9,
                               geometry, tags.get("name", ""))

    Handler().apply_file(str(source), locations=True, idx=location_index)
    writer.flush_addresses()
    writer.flush_features()


def build_index(source: str | Path, output: str | Path, *, force: bool = False,
                location_index: str = "flex_mem") -> dict:
    """Build atomically, or reuse a complete index of the same source and schema."""
    started = time.monotonic()
    source, output = Path(source), Path(output)
    if source.resolve() == output.resolve():
        raise ValueError("Source and output must be different files")
    metadata = _source_metadata(source)
    existing = None if force else _existing_metadata(output, metadata)
    if existing is not None:
        return {**existing, "reused": True, "elapsed_seconds": round(time.monotonic() - started, 3)}
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{output.name}.", suffix=".tmp",
                                            dir=output.parent)
    os.close(descriptor)
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.executescript(SCHEMA)
            writer = _IndexWriter(connection)
            _import_osm(source, writer, location_index)
            current = source.stat()
            if (str(current.st_size), str(current.st_mtime_ns)) != (
                    metadata["source_size"], metadata["source_mtime_ns"]):
                raise RuntimeError("OSM source changed during indexing; run preparation again")
            metadata.update({"bounds": json.dumps(writer.bounds),
                             "address_count": str(writer.address_count),
                             "feature_count": str(writer.feature_count),
                             "built_at": datetime.now(UTC).isoformat()})
            connection.execute("INSERT INTO addresses_fts(addresses_fts) VALUES ('rebuild')")
            connection.executemany("INSERT INTO metadata VALUES (?,?)", metadata.items())
            connection.execute("ANALYZE")
            connection.commit()
        # Ensure complete SQLite data reaches disk before replacing the previous index.
        with open(temporary, "rb") as stream:
            os.fsync(stream.fileno())
        # The preparation container and API may run under different users.
        os.chmod(temporary, 0o644)
        os.replace(temporary, output)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return {**metadata, "reused": False, "elapsed_seconds": round(time.monotonic() - started, 3)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="Local .osm.pbf or .osm file")
    parser.add_argument("--output", required=True, type=Path, help="SQLite address/map index")
    parser.add_argument("--force", action="store_true", help="Rebuild even when source is unchanged")
    parser.add_argument("--location-index", default="flex_mem", help="Pyosmium node cache backend")
    args = parser.parse_args()
    try:
        result = build_index(args.source, args.output, force=args.force,
                             location_index=args.location_index)
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, f"Local map preparation failed: {exc}\n")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
