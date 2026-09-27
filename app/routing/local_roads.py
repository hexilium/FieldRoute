"""Local OSM distance matrices; fixed speeds, no traffic or transit timetables."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from itertools import pairwise
from math import inf, isfinite
from pathlib import Path

import httpx

from app.config import Settings
from app.domain.models import Engineer, JobStatus, Location, PlanRequest
from app.routing.base import RoutingProvider
from app.services.planning_settings import assumed_speeds

NETWORKS = {"car": "car", "public_transport": "car", "walk": "foot", "bicycle": "bicycle"}


class RoutingUnavailable(RuntimeError):
    pass


class RoutingCoverageError(ValueError):
    pass


class RoadCache:
    """Disk cache shared by planning, validation and later replanning requests."""

    def __init__(self, path: Path):
        self.path = path
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS road_cache "
                               "(namespace TEXT, key TEXT, value TEXT, "
                               "PRIMARY KEY(namespace, key))")

    def connect(self):
        from contextlib import closing
        return closing(sqlite3.connect(self.path, timeout=30))

    def read(self, namespace: str, keys: list[str]) -> dict:
        result = {}
        with self.connect() as connection:
            for offset in range(0, len(keys), 400):
                batch = keys[offset:offset + 400]
                marks = ",".join("?" for _ in batch)
                for key, value in connection.execute(
                    f"SELECT key, value FROM road_cache WHERE namespace=? AND key IN ({marks})",
                    [namespace, *batch],
                ):
                    result[key] = json.loads(value)
        return result

    def write(self, namespace: str, values: dict) -> None:
        with self.connect() as connection:
            connection.executemany("INSERT OR REPLACE INTO road_cache VALUES (?, ?, ?)",
                                   [(namespace, key, json.dumps(value, allow_nan=False))
                                    for key, value in values.items()])
            connection.commit()


class LocalRoadsRoutingProvider(RoutingProvider):
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        directory = Path(settings.road_data_dir)
        try:
            signature = (directory / "ready").read_bytes()
        except OSError as exc:
            raise RoutingUnavailable(
                "Дорожные данные Москвы ещё не подготовлены. Запустите make run "
                "или docker compose up --build и дождитесь подготовки карты."
            ) from exc
        self.version = hashlib.sha256(signature).hexdigest()
        self.urls = {"car": settings.roads_car_url.rstrip("/"),
                     "foot": settings.roads_foot_url.rstrip("/"),
                     "bicycle": settings.roads_bicycle_url.rstrip("/")}
        self.speeds_kmh = assumed_speeds(settings)
        self.radius = settings.roads_snap_radius_m
        self.batch_size = settings.roads_matrix_batch_size
        self.matrix_concurrency = settings.roads_matrix_concurrency
        self.transport = transport
        self.cache = RoadCache(directory / "distances.sqlite3")
        self.distances: dict[str, dict[str, float]] = {p: {} for p in self.urls}
        self.table_requests = 0
        self.geometry_requests = 0
        self.disk_hits = 0

    def validate_engineer(self, engineer: Engineer) -> None:
        pass  # All four modes and optional speeds are validated by Engineer.

    @staticmethod
    def point(point: Location) -> str:
        return f"{point.lon:.6f},{point.lat:.6f}"

    def namespace(self, profile: str, kind: str) -> str:
        return f"v1:{self.version}:{self.urls[profile]}:{self.radius}:{profile}:{kind}"

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=60, transport=self.transport)

    async def query(self, client, profile: str, service: str, points: list[str], **params) -> dict:
        url = f"{self.urls[profile]}/{service}/v1/{profile}/" + ";".join(points)
        params["radiuses"] = ";".join([str(self.radius)] * len(points))
        try:
            response = await client.get(url, params=params)
            payload = response.json()
            if not isinstance(payload, dict):
                raise TypeError("Invalid routing response")
            if payload.get("code") == "NoSegment":
                raise RoutingCoverageError(
                    f"Для профиля {profile} есть точки вне дорожной сети Москвы "
                    f"или дальше {self.radius:g} м от доступного пути. Проверьте координаты."
                )
            response.raise_for_status()
            if payload.get("code") not in {"Ok", "NoRoute", "NoTable"}:
                raise ValueError(payload.get("code", "no code"))
            return payload
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            if isinstance(exc, RoutingCoverageError):
                raise
            raise RoutingUnavailable(
                f"Локальный маршрутизатор {profile} недоступен или вернул ошибку. "
                "Проверьте запуск дорожных сервисов: make roads."
            ) from exc

    def load(self, profile: str, keys: list[str]) -> None:
        missing = [key for key in keys if key not in self.distances[profile]]
        cached = self.cache.read(self.namespace(profile, "distance"), missing)
        self.disk_hits += len(cached)
        self.distances[profile].update({key: inf if value < 0 else value
                                        for key, value in cached.items()})

    async def table(self, client, profile: str, origins: list[str], destinations: list[str]):
        points = list(dict.fromkeys([*origins, *destinations]))
        indices = {point: index for index, point in enumerate(points)}
        # OSRM requires at least two coordinates, even for a same-location pair.
        # Keep the request so off-network points still fail the coverage check.
        if len(points) == 1:
            points.append(points[0])
        payload = await self.query(
            client, profile, "table", points, annotations="distance",
            sources=";".join(str(indices[p]) for p in origins),
            destinations=";".join(str(indices[p]) for p in destinations),
        )
        self.table_requests += 1
        matrix = payload.get("distances")
        if payload.get("code") in {"NoRoute", "NoTable"}:
            matrix = [[None] * len(destinations) for _ in origins]
        if not isinstance(matrix, list) or len(matrix) != len(origins) or any(
            not isinstance(row, list) or len(row) != len(destinations) for row in matrix
        ):
            raise RoutingUnavailable("Маршрутизатор вернул неполную матрицу расстояний.")
        values = {}
        for i, a in enumerate(origins):
            for j, b in enumerate(destinations):
                value = matrix[i][j]
                if value is not None and (
                    not isinstance(value, (int, float)) or not isfinite(value) or value < 0
                ):
                    raise RoutingUnavailable("Маршрутизатор вернул неверное расстояние.")
                values[a + ";" + b] = -1 if value is None else value / 1000
        self.cache.write(self.namespace(profile, "distance"), values)
        self.distances[profile].update({key: inf if value < 0 else value
                                        for key, value in values.items()})

    async def prepare(self, request: PlanRequest) -> None:
        if request.district_mode == "strict":
            from app.solvers.districts import district_requests

            for _, child in district_requests(request):
                await self._prepare_flat(child)
            return
        await self._prepare_flat(request)

    async def _prepare_flat(self, request: PlanRequest) -> None:
        jobs = [j for j in request.jobs if j.status not in {JobStatus.completed, JobStatus.cancelled}]
        if not jobs or not request.engineers:
            return
        job_points = [self.point(j.location) for j in jobs]
        executing_jobs = {j.id: j.location for j in jobs if j.status == JobStatus.in_progress}
        executing_origins = {
            route.engineer_id: executing_jobs[stop.job_id]
            for route in (request.previous_plan.routes if request.previous_plan else [])
            for stop in route.stops if stop.job_id in executing_jobs
        }
        batches = []
        async with self.client() as client:
            for profile in sorted({NETWORKS[e.travel_mode] for e in request.engineers}):
                starts = [self.point(executing_origins.get(e.id) or e.current_location or e.start_location)
                          for e in request.engineers if NETWORKS[e.travel_mode] == profile]
                points = list(dict.fromkeys([*job_points, *starts]))
                # Only job locations are destinations; engineers' start locations
                # are origins. Each directed pair has its own entry.
                destinations = list(dict.fromkeys(job_points))
                self.load(profile, [a + ";" + b for a in points for b in destinations])
                for offset in range(0, len(points), self.batch_size):
                    origins = points[offset:offset + self.batch_size]
                    for end in range(0, len(destinations), self.batch_size):
                        targets = destinations[end:end + self.batch_size]
                        # Drop already-cached rows/columns, so a new job only adds
                        # its missing row and column rather than rebuilding N².
                        sources = [a for a in origins if any(
                            a + ";" + b not in self.distances[profile] for b in targets)]
                        missing_targets = [b for b in targets if any(
                            a + ";" + b not in self.distances[profile] for a in sources)]
                        if sources and missing_targets:
                            batches.append((profile, sources, missing_targets))

            # A fixed number of consumers bounds both live HTTP requests and
            # asyncio tasks. Each batch contains disjoint directed pairs.
            pending = iter(batches)

            async def consume():
                for profile, sources, targets in pending:
                    await self.table(client, profile, sources, targets)

            workers = [asyncio.create_task(consume())
                       for _ in range(min(self.matrix_concurrency, len(batches)))]
            try:
                await asyncio.gather(*workers)
            finally:
                # On an error/disconnect close outstanding requests before client exit.
                for worker in workers:
                    worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

    async def distance(self, a: Location, b: Location, profile: str) -> float:
        origin, destination = self.point(a), self.point(b)
        key = origin + ";" + destination
        if key not in self.distances[profile]:
            self.load(profile, [key])
        if key not in self.distances[profile]:
            async with self.client() as client:
                await self.table(client, profile, [origin], [destination])
        return self.distances[profile][key]

    async def distance_time_for_engineer(self, a, b, engineer):
        km = await self.distance(a, b, NETWORKS[engineer.travel_mode])
        speed = engineer.travel_speed_kmh or self.speeds_kmh[engineer.travel_mode]
        return km, km / speed * 60

    def arrival_tolerance_seconds(self, engineer: Engineer) -> float:
        # OSRM table distances can differ by one 0.1 m rounding step between
        # batched planning and pairwise restoration with a fresh cache.
        speed = engineer.travel_speed_kmh or self.speeds_kmh[engineer.travel_mode]
        return max(super().arrival_tolerance_seconds(engineer), 0.0001 / speed * 3600 + 0.000001)

    async def distance_time(self, a, b):
        km = await self.distance(a, b, "car")
        return km, km / self.speeds_kmh["car"] * 60

    async def route_geometry_for_engineer(self, locations, engineer):
        return await self.geometry(locations, NETWORKS[engineer.travel_mode])

    async def route_geometry(self, locations):
        return await self.geometry(locations, "car")

    async def geometry(self, locations: list[Location], profile: str) -> list[Location]:
        if len(locations) < 2:
            return list(locations)
        points = [self.point(p) for p in locations]
        pairs = list(pairwise(points))
        keys = [a + ";" + b for a, b in pairs]
        namespace = self.namespace(profile, "geometry")
        cached = self.cache.read(namespace, keys)
        result = []
        async with self.client() as client:
            for (a, b), key in zip(pairs, keys):
                coordinates = cached.get(key)
                if coordinates is None:
                    payload = await self.query(client, profile, "route", [a, b],
                                               overview="full", geometries="geojson", steps="false")
                    self.geometry_requests += 1
                    if payload.get("code") != "Ok" or not payload.get("routes"):
                        raise RoutingUnavailable("Не удалось построить геометрию выбранного пути.")
                    coordinates = payload["routes"][0].get("geometry", {}).get("coordinates")
                    if not coordinates:
                        raise RoutingUnavailable("Маршрутизатор не вернул геометрию пути.")
                    self.cache.write(namespace, {key: coordinates})
                    cached[key] = coordinates
                segment = [Location(lon=p[0], lat=p[1]) for p in coordinates]
                result.extend(segment[1:] if result and result[-1] == segment[0] else segment)
        return result

    @property
    def stats(self):
        return {"table_requests": self.table_requests, "geometry_requests": self.geometry_requests,
                "disk_hits": self.disk_hits,
                "distance_entries": sum(len(v) for v in self.distances.values())}
