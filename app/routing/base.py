from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.models import Engineer, Location, PlanRequest


class UnsupportedTravelProfile(ValueError):
    pass


class RoutingProvider(ABC):
    async def prepare(self, request: PlanRequest) -> None:
        """Optionally batch distances before entering the scheduling search."""

    async def route_geometry_for_engineer(
        self, locations: list[Location], engineer: Engineer,
    ) -> list[Location]:
        return await self.route_geometry(locations)

    def validate_engineer(self, engineer: Engineer) -> None:
        if engineer.travel_mode != "car" or engineer.travel_speed_kmh is not None:
            raise UnsupportedTravelProfile(
                "Этот маршрутизатор поддерживает только автомобиль без заданной скорости. "
                "Для других видов транспорта и оценки по скорости используйте Haversine."
            )

    async def distance_time_for_engineer(
        self, a: Location, b: Location, engineer: Engineer
    ) -> tuple[float, float]:
        self.validate_engineer(engineer)
        return await self.distance_time(a, b)

    def arrival_tolerance_seconds(self, engineer: Engineer) -> float:
        """Numerical tolerance for independently recomputed arrival times."""
        return 0.01

    @abstractmethod
    async def distance_time(self, a: Location, b: Location) -> tuple[float, float]:
        """Return (distance_km, travel_minutes)."""

    async def matrix(
        self, locations: list[Location]
    ) -> tuple[list[list[float]], list[list[float]]]:
        """Return (distance_km_matrix, travel_minutes_matrix).

        Concrete routing engines can override this with a batch API (OSRM Table). The default
        keeps the interface usable for simple or test providers.
        """
        distances: list[list[float]] = []
        durations: list[list[float]] = []
        for origin in locations:
            distance_row: list[float] = []
            duration_row: list[float] = []
            for destination in locations:
                km, minutes = await self.distance_time(origin, destination)
                distance_row.append(km)
                duration_row.append(minutes)
            distances.append(distance_row)
            durations.append(duration_row)
        return distances, durations
    async def route_geometry(self, locations: list[Location]) -> list[Location]:
        """Return route polyline points. Default is a straight stop-to-stop geometry."""
        return list(locations)
