from __future__ import annotations

from math import asin, cos, isfinite, radians, sin, sqrt

from app.domain.models import Engineer, Location
from app.routing.base import RoutingProvider

# Scenario assumptions, not measured speeds or timetable forecasts.
ASSUMED_SPEEDS_KMH = {"car": 28.0, "walk": 5.0, "bicycle": 15.0, "public_transport": 18.0}


class HaversineRoutingProvider(RoutingProvider):
    """No-network distance and travel-time estimate with fixed per-engineer modes."""

    def __init__(self, average_speed_kmh: float = 28.0, road_factor: float = 1.25, *,
                 speeds_kmh: dict[str, float] | None = None) -> None:
        if not all(isfinite(x) and x > 0 for x in (average_speed_kmh, road_factor)):
            raise ValueError("Speed and road factor must be finite and positive")
        self.average_speed_kmh = average_speed_kmh
        self.road_factor = road_factor
        self.speeds_kmh = dict(ASSUMED_SPEEDS_KMH)
        if speeds_kmh is not None:
            if set(speeds_kmh) != set(ASSUMED_SPEEDS_KMH) or not all(
                isfinite(value) and 0 < value <= 200 for value in speeds_kmh.values()
            ):
                raise ValueError("All four finite positive transport speeds are required")
            self.speeds_kmh.update(speeds_kmh)

    def validate_engineer(self, engineer: Engineer) -> None:
        # Engineer validates the mode and optional finite, positive speed.
        pass

    async def distance_time_for_engineer(
        self, a: Location, b: Location, engineer: Engineer
    ) -> tuple[float, float]:
        speed = engineer.travel_speed_kmh or (
            self.average_speed_kmh if engineer.travel_mode == "car"
            else self.speeds_kmh[engineer.travel_mode]
        )
        km = self._air_distance_km(a, b) * self.road_factor
        return km, km / speed * 60

    @staticmethod
    def _air_distance_km(a: Location, b: Location) -> float:
        r = 6371.0088
        lat1, lon1, lat2, lon2 = map(radians, [a.lat, a.lon, b.lat, b.lon])
        dlat = lat2 - lat1
        dlon = lon2 - lon1
        h = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
        return 2 * r * asin(sqrt(h))

    async def distance_time(self, a: Location, b: Location) -> tuple[float, float]:
        km = self._air_distance_km(a, b) * self.road_factor
        minutes = (km / self.average_speed_kmh) * 60 if self.average_speed_kmh > 0 else 0
        return km, minutes

    async def matrix(
        self, locations: list[Location]
    ) -> tuple[list[list[float]], list[list[float]]]:
        distances: list[list[float]] = []
        durations: list[list[float]] = []
        for a in locations:
            distance_row: list[float] = []
            duration_row: list[float] = []
            for b in locations:
                km = self._air_distance_km(a, b) * self.road_factor
                minutes = (km / self.average_speed_kmh) * 60 if self.average_speed_kmh > 0 else 0
                distance_row.append(km)
                duration_row.append(minutes)
            distances.append(distance_row)
            durations.append(duration_row)
        return distances, durations
