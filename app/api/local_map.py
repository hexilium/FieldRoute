"""Serve bounded map views directly from the prepared local OSM index."""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.config import Settings, get_settings

router = APIRouter(prefix="/api/v1/map")
ATTRIBUTION = "© OpenStreetMap contributors"
MAX_FEATURES = 3000
MAX_GEOMETRY_BYTES = 2_000_000
Longitude = Annotated[float, Query(ge=-180, le=180, allow_inf_nan=False)]
Latitude = Annotated[float, Query(ge=-90, le=90, allow_inf_nan=False)]


def index_path(settings: Settings) -> Path:
    return Path(settings.geodata_path or Path(settings.road_data_dir) / "geodata.sqlite3")


def connect(settings: Settings):
    # mode=ro never creates an empty index when preparation has not run yet.
    path = index_path(settings).resolve()
    return closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2))


@router.get("/status")
def status(settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    if settings.roads_remote:
        return {"available": True, "backend": "online", "bounds": None,
                "source": "OpenStreetMap · онлайн", "attribution": ATTRIBUTION,
                "tile_url": settings.online_tiles_url}
    result = {
        "available": False, "bounds": None, "address_count": 0, "feature_count": 0,
        "source": "Локальная карта OpenStreetMap", "attribution": ATTRIBUTION,
    }
    try:
        with connect(settings) as connection:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            west, south, east, north = json.loads(metadata["bounds"])
            connection.execute("SELECT id FROM features_rtree LIMIT 1").fetchone()
            result.update(
                available=True, bounds=[[south, west], [north, east]],
                address_count=int(metadata["address_count"]),
                feature_count=int(metadata["feature_count"]),
            )
    except (OSError, sqlite3.Error, KeyError, ValueError, TypeError):
        result["detail"] = "Локальная карта не подготовлена. Выполните make geodata."
    return result


@router.get("/features")
def features(
    west: Longitude, south: Latitude, east: Longitude, north: Latitude,
    zoom: Annotated[int, Query(ge=1, le=19)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict:
    if west >= east or south >= north:
        raise HTTPException(422, "Некорректные границы карты.")
    output = []
    size = 0
    truncated = False
    try:
        with connect(settings) as connection:
            rows = connection.execute(
                "SELECT f.id, f.kind, f.geometry, f.name FROM features_rtree r "
                "JOIN features f ON f.id=r.id "
                "WHERE r.max_lon>=? AND r.min_lon<=? AND r.max_lat>=? "
                "AND r.min_lat<=? AND f.min_zoom<=? "
                "ORDER BY f.min_zoom, f.id LIMIT ?",
                (west, east, south, north, zoom, MAX_FEATURES + 1),
            )
            for identifier, kind, geometry, name in rows:
                size += len(geometry)
                if len(output) >= MAX_FEATURES or size > MAX_GEOMETRY_BYTES:
                    truncated = True
                    break
                output.append({
                    "type": "Feature", "id": identifier,
                    "geometry": json.loads(geometry),
                    "properties": {"kind": kind, "name": name},
                })
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise HTTPException(
            503, "Локальная карта недоступна. Подготовьте индекс командой make geodata.",
        ) from exc
    return {
        "type": "FeatureCollection", "features": output,
        "truncated": truncated, "attribution": ATTRIBUTION,
    }
