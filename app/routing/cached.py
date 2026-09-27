from __future__ import annotations

from collections import OrderedDict

from app.domain.models import Engineer, Location
from app.routing.base import RoutingProvider


class CachedRoutingProvider(RoutingProvider):
    """Small in-memory cache for one planning process."""

    def __init__(
        self, delegate: RoutingProvider, precision: int = 6, *, point_cache_size: int = 4096,
    ) -> None:
        if point_cache_size < 0:
            raise ValueError("point_cache_size must be nonnegative")
        self.delegate = delegate
        self.precision = precision
        self.point_cache_size = point_cache_size
        self._point_cache: OrderedDict[tuple, tuple[float, float]] = OrderedDict()
        self._cache: dict[tuple, tuple[float, float]] = {}
        self.hits = 0
        self.misses = 0

    def _point_key(self, point: Location) -> tuple[float, float]:
        # Key by coordinates, not object identity: mutable Location objects and
        # precision changes must retain the original rounding behavior.
        key = point.lat, point.lon, self.precision
        value = self._point_cache.get(key)
        if value is None:
            value = round(key[0], key[2]), round(key[1], key[2])
            if self.point_cache_size:
                if len(self._point_cache) >= self.point_cache_size:
                    self._point_cache.popitem(last=False)
                self._point_cache[key] = value
        return value

    def _key(self, a: Location, b: Location) -> tuple[float, float, float, float]:
        return *self._point_key(a), *self._point_key(b)

    async def distance_time(self, a: Location, b: Location) -> tuple[float, float]:
        key = self._key(a, b)
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        value = await self.delegate.distance_time(a, b)
        self._cache[key] = value
        self.misses += 1
        return value

    def validate_engineer(self, engineer: Engineer) -> None:
        self.delegate.validate_engineer(engineer)

    def arrival_tolerance_seconds(self, engineer: Engineer) -> float:
        return self.delegate.arrival_tolerance_seconds(engineer)

    async def prepare(self, request) -> None:
        await self.delegate.prepare(request)

    async def route_geometry_for_engineer(self, locations, engineer):
        return await self.delegate.route_geometry_for_engineer(locations, engineer)

    async def distance_time_for_engineer(
        self, a: Location, b: Location, engineer: Engineer
    ) -> tuple[float, float]:
        self.validate_engineer(engineer)
        key = (engineer.travel_mode, engineer.travel_speed_kmh, *self._key(a, b))
        cached = self._cache.get(key)
        if cached is not None:
            self.hits += 1
            return cached
        value = await self.delegate.distance_time_for_engineer(a, b, engineer)
        self._cache[key] = value
        self.misses += 1
        return value

    async def matrix(
        self, locations: list[Location]
    ) -> tuple[list[list[float]], list[list[float]]]:
        distances, durations = await self.delegate.matrix(locations)
        for i, a in enumerate(locations):
            for j, b in enumerate(locations):
                self._cache[self._key(a, b)] = (distances[i][j], durations[i][j])
        self.misses += len(locations) * len(locations)
        return distances, durations

    @property
    def stats(self) -> dict[str, int]:
        result = {"hits": self.hits, "misses": self.misses, "size": len(self._cache),
                  "point_entries": len(self._point_cache), "max_point_entries": self.point_cache_size}
        if hasattr(self.delegate, "stats"):
            result.update(self.delegate.stats)
        return result
    async def route_geometry(self, locations: list[Location]) -> list[Location]:
        return await self.delegate.route_geometry(locations)
