"""Address search and reverse lookup using the prepared regional SQLite index."""
from __future__ import annotations

import asyncio
import math
import re
import sqlite3
from contextlib import closing, nullcontext
from functools import lru_cache
from pathlib import Path

from pydantic import ValidationError

from app.config import Settings
from app.geodata.normalize import normalize_address
from app.services.geocoding import GeocodedPlace, GeocodingError

EARTH_RADIUS_M = 6_371_000
STREET_TYPES = {"улица", "проспект", "переулок", "бульвар", "набережная", "площадь",
                "шоссе", "проезд", "аллея"}
INDEX_UNAVAILABLE = (
    "Локальный индекс адресов не подготовлен или недоступен. "
    "Подготовьте геоданные региона и проверьте GEODATA_PATH. "
    "Координаты можно выбрать на карте или ввести вручную."
)


def geodata_path(settings: Settings) -> Path:
    return Path(settings.geodata_path or Path(settings.road_data_dir) / "geodata.sqlite3")


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lat2 = math.radians(lat1), math.radians(lat2)
    latitude_delta = lat2 - lat1
    longitude_delta = math.radians(lon2 - lon1)
    value = (math.sin(latitude_delta / 2) ** 2
             + math.cos(lat1) * math.cos(lat2) * math.sin(longitude_delta / 2) ** 2)
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(min(1.0, max(0.0, value))))


def explicit_house_number(query: str) -> str | None:
    """Recognize a terminal house after a comma or д/дом, preserving street ambiguity."""
    for match in re.finditer(r"(?:,|\b(?:дом|д)\.?\s*)(?=\s*\d)", query.casefold()):
        number = normalize_address(query[match.end():])
        if re.fullmatch(r"\d[^\W_]*", number):
            return number
    return None


class LocalGeocodingService:
    def __init__(self, settings: Settings):
        self.path = geodata_path(settings)
        self.reverse_radius_m = settings.local_geocoding_radius_m

    def connect(self) -> sqlite3.Connection:
        # Read-only mode also prevents silently creating an empty index on a typo.
        connection = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('addresses', 'addresses_fts', 'addresses_rtree')",
            )}
            if len(tables) != 3:
                raise GeocodingError(INDEX_UNAVAILABLE)
            if not connection.execute("SELECT 1 FROM addresses LIMIT 1").fetchone():
                raise GeocodingError(
                    "В локальном индексе нет адресов. Подготовьте геоданные нужного региона. "
                    "Координаты можно выбрать на карте или ввести вручную."
                )
        except Exception:
            connection.close()
            raise
        return connection

    async def search(self, query: str, lat: float | None, lon: float | None) -> list[dict]:
        return await asyncio.to_thread(self._search, query, lat, lon)

    def _search(self, query: str, lat: float | None, lon: float | None, *,
                connection: sqlite3.Connection | None = None) -> list[dict]:
        # OSM sometimes stores the street without its generic "улица" type.
        # The name and house are sufficient; other types (e.g. проспект) stay explicit.
        terms = [term for term in normalize_address(query).split() if term != "улица"]
        if not terms:
            return []
        # Only the last word may be incomplete; house numbers always match exactly.
        prefix = terms[-1].isalpha() and len(terms[-1]) >= 2
        expression = " AND ".join('"' + term + '"' for term in terms)
        if prefix:
            expression += "*"
        conditions = ["addresses_fts MATCH ?"]
        params: list = [expression]
        house = explicit_house_number(query)
        if house is not None:
            # A number in the street name must not satisfy an explicit house:
            # "Марта, 8" cannot resolve to "8 Марта, 5".
            conditions.append("geocoding_normalize(a.housenumber)=?")
            params.append(house)
        order = ["CASE WHEN instr(' ' || a.search_text || ' ', ?) > 0 THEN 0 ELSE 1 END"]
        params.append(" " + terms[-1] + " ")

        @lru_cache(maxsize=4096)
        def street_score(street: str) -> int:
            street_terms = set(normalize_address(street).split()) - STREET_TYPES
            matched = {word for word in street_terms
                       if word in terms or (prefix and word.startswith(terms[-1]))}
            if not any(any(char.isalpha() for char in word) for word in matched):
                return 0
            # "Тверская" is a closer textual match than "1-я Тверская-Ямская",
            # even when the latter happens to be closer to the map center.
            return len(street_terms - matched)

        order.append("geocoding_street_score(a.street)")
        if lat is not None and lon is not None:
            # Every required token already matched. Among equally exact matches,
            # prefer the current map area, while keeping all address choices visible.
            order.append("geocoding_distance(a.lat, a.lon, ?, ?)")
            params.extend([lat, lon])
        order.extend(["bm25(addresses_fts)", "a.label", "a.id"])
        try:
            with (nullcontext(connection) if connection is not None else closing(self.connect())) as database:
                database.create_function("geocoding_distance", 4, distance_m, deterministic=True)
                database.create_function("geocoding_street_score", 1, street_score,
                                           deterministic=True)
                database.create_function("geocoding_normalize", 1, normalize_address,
                                           deterministic=True)
                rows = database.execute(
                    "SELECT a.lat, a.lon, a.label, a.street, a.housenumber, a.city "
                    "FROM addresses_fts "
                    "JOIN addresses a ON a.id=addresses_fts.rowid "
                    "WHERE " + " AND ".join(conditions)
                    + " ORDER BY " + ", ".join(order) + " LIMIT 250",
                    params,
                ).fetchall()
                return [self.place(row) for row in self.distinct_addresses(rows)[:5]]
        except (OSError, sqlite3.Error, ValueError, TypeError, ValidationError) as exc:
            raise GeocodingError(INDEX_UNAVAILABLE) from exc

    @staticmethod
    def distinct_addresses(rows: list[sqlite3.Row]) -> list[sqlite3.Row]:
        """Collapse nearby POI/building copies without merging different towns."""
        selected: list[sqlite3.Row] = []
        groups: dict[tuple, list[int]] = {}
        cities: list[str] = []
        for row in rows:
            key = (tuple(sorted(normalize_address(row["street"]).split())),
                   normalize_address(row["housenumber"]))
            city = normalize_address(row["city"])
            for index in groups.get(key, []):
                previous, previous_city = selected[index], cities[index]
                if ((not city or not previous_city or city == previous_city)
                        and distance_m(row["lat"], row["lon"], previous["lat"], previous["lon"])
                        <= 150):
                    if city and not previous_city:
                        selected[index] = row
                        cities[index] = city
                    break
            else:
                groups.setdefault(key, []).append(len(selected))
                selected.append(row)
                cities.append(city)
        return selected

    async def reverse(self, lat: float, lon: float) -> dict | None:
        return await asyncio.to_thread(self._reverse, lat, lon)

    def _reverse(self, lat: float, lon: float) -> dict | None:
        angular_radius = self.reverse_radius_m / EARTH_RADIUS_M
        latitude_delta = math.degrees(angular_radius)
        min_lat, max_lat = max(-90, lat - latitude_delta), min(90, lat + latitude_delta)
        if min_lat == -90 or max_lat == 90:
            ranges = [(-180, 180)]
        else:
            longitude_delta = math.degrees(math.asin(min(
                1.0, math.sin(angular_radius) / math.cos(math.radians(lat)),
            )))
            left, right = lon - longitude_delta, lon + longitude_delta
            if left < -180:
                ranges = [(-180, right), (left + 360, 180)]
            elif right > 180:
                ranges = [(left, 180), (-180, right - 360)]
            else:
                ranges = [(left, right)]
        try:
            with closing(self.connect()) as connection:
                candidates = []
                for left, right in ranges:
                    candidates.extend(connection.execute(
                        "SELECT a.lat, a.lon, a.label FROM addresses_rtree r "
                        "JOIN addresses a ON a.id=r.id "
                        "WHERE r.max_lat>=? AND r.min_lat<=? "
                        "AND r.max_lon>=? AND r.min_lon<=?",
                        (min_lat, max_lat, left, right),
                    ).fetchall())
                nearby = [(distance_m(lat, lon, row["lat"], row["lon"]), row)
                          for row in candidates]
                nearby = [item for item in nearby if item[0] <= self.reverse_radius_m]
                if not nearby:
                    return None
                nearest = min(nearby, key=lambda item: (item[0], item[1]["label"]))[1]
                return self.place(nearest)
        except (OSError, sqlite3.Error, ValueError, TypeError, ValidationError) as exc:
            raise GeocodingError(INDEX_UNAVAILABLE) from exc

    @staticmethod
    def place(row: sqlite3.Row) -> dict:
        return GeocodedPlace(lat=row["lat"], lon=row["lon"], label=row["label"]).model_dump()
