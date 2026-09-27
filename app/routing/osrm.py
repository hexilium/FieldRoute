from __future__ import annotations

import httpx

from app.domain.models import Location
from app.routing.base import RoutingProvider


class OsrmRoutingProvider(RoutingProvider):
    def __init__(self, base_url: str, timeout_seconds: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def distance_time(self, a: Location, b: Location) -> tuple[float, float]:
        coordinates = f"{a.lon},{a.lat};{b.lon},{b.lat}"
        url = f"{self.base_url}/route/v1/driving/{coordinates}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(url, params={"overview": "false"})
            response.raise_for_status()
            payload = response.json()
        if payload.get("code") == "NoRoute":
            return float("inf"), float("inf")
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise RuntimeError(f"OSRM route error: {payload.get('message', payload.get('code'))}")
        route = payload["routes"][0]
        return route["distance"] / 1000.0, route["duration"] / 60.0

    async def matrix(
        self, locations: list[Location]
    ) -> tuple[list[list[float]], list[list[float]]]:
        if not locations:
            return [], []
        coordinates = ";".join(f"{p.lon},{p.lat}" for p in locations)
        url = f"{self.base_url}/table/v1/driving/{coordinates}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(url, params={"annotations": "distance,duration"})
            response.raise_for_status()
            payload = response.json()
        if payload.get("code") != "Ok":
            raise RuntimeError(f"OSRM table error: {payload.get('message', payload.get('code'))}")
        distance_matrix = [
            [float("inf") if value is None else value / 1000.0 for value in row]
            for row in payload.get("distances", [])
        ]
        duration_matrix = [
            [float("inf") if value is None else value / 60.0 for value in row]
            for row in payload.get("durations", [])
        ]
        return distance_matrix, duration_matrix
    async def route_geometry(self, locations: list[Location]) -> list[Location]:
        if len(locations) < 2:
            return list(locations)
        coordinates = ";".join(f"{p.lon},{p.lat}" for p in locations)
        url = f"{self.base_url}/route/v1/driving/{coordinates}"
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                url,
                params={"overview": "full", "geometries": "geojson", "steps": "false"},
            )
            response.raise_for_status()
            payload = response.json()
        if payload.get("code") != "Ok" or not payload.get("routes"):
            return list(locations)
        coords = payload["routes"][0].get("geometry", {}).get("coordinates", [])
        return [Location(lat=lat, lon=lon) for lon, lat in coords]
