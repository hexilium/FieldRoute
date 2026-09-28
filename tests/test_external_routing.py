import asyncio
import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from app.api.local_map import status
from app.config import Settings
from app.domain.models import Engineer, Location, TimeWindow
from app.routing.local_roads import (
    LocalRoadsRoutingProvider,
    RoutingUnavailable,
    remote_get,
)

ROOT = Path(__file__).resolve().parents[1]
A = Location(lat=55.75, lon=37.61)
B = Location(lat=55.76, lon=37.62)


def settings(tmp_path, **kwargs):
    return Settings(roads_remote=True, routing_backend="local_roads",
                    road_data_dir=str(tmp_path / "no-map"),
                    road_cache_dir=str(tmp_path / "cache"), **kwargs)


@pytest.mark.parametrize("mode,profile", [("car", "car"), ("walk", "foot"),
                                          ("bicycle", "bicycle"), ("public_transport", "car")])
def test_external_profiles_speeds_and_persistent_cache(tmp_path, mode, profile):
    calls = []
    def respond(req):
        calls.append(req)
        assert f"/table/v1/{profile}/" in req.url.path
        assert req.headers["User-Agent"].startswith("FieldRoute/")
        return httpx.Response(200, json={"code": "Ok", "distances": [[2000]]})
    cfg = settings(tmp_path)
    transport = httpx.MockTransport(respond)
    day = datetime(2026, 9, 28, tzinfo=UTC)
    engineer = Engineer(id="e", name="E", start_location=A, transport_modes={mode},
                        travel_speed_kmh=10, shift=TimeWindow(start=day, end=day+timedelta(hours=8)))
    async def run():
        for _ in range(2):
            provider = LocalRoadsRoutingProvider(cfg, transport=transport)
            assert await provider.distance_time_for_engineer(A, B, engineer) == (2, 12)
    asyncio.run(run())
    assert len(calls) == 1
    assert not (tmp_path / "no-map").exists()


def test_local_mode_still_requires_graph(tmp_path):
    cfg = settings(tmp_path).model_copy(update={"roads_remote": False})
    with pytest.raises(RoutingUnavailable, match="не подготовлены"):
        LocalRoadsRoutingProvider(cfg)


def test_external_failure_does_not_fall_back_to_straight_lines(tmp_path):
    provider = LocalRoadsRoutingProvider(settings(tmp_path), transport=httpx.MockTransport(
        lambda req: httpx.Response(429, json={"code": "TooManyRequests"})))
    with pytest.raises(RoutingUnavailable, match="Внешний"):
        asyncio.run(provider.distance(A, B, "car"))


def test_online_map_needs_no_sqlite_index(tmp_path):
    data = status(settings(tmp_path))
    assert data["available"] and data["backend"] == "online"
    assert data["tile_url"] == "https://tile.openstreetmap.org/{z}/{x}/{y}.png"


def test_shared_remote_rate_limit():
    starts = []
    def respond(req):
        starts.append(time.monotonic())
        return httpx.Response(200)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await asyncio.gather(remote_get(client, "https://example.org/car", {}),
                                 remote_get(client, "https://example.org/foot", {}))
    asyncio.run(run())
    assert starts[1] - starts[0] >= 1.0


def test_external_compose_has_no_local_dependencies():
    result = subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.external.yml"),
                             "config", "--format", "json"], check=True, capture_output=True,
                            text=True, env={**os.environ, "FIELDROUTE_DOMAIN": "demo.example.org",
                                            "FIELDROUTE_PASSWORD_HASH": "test"})
    services = json.loads(result.stdout)["services"]
    assert set(services) == {"api", "caddy"}
    assert not services["api"].get("depends_on")
    assert not services["api"].get("ports")
    assert services["api"]["environment"]["GEOCODING_BACKEND"] == "photon"
    assert services["api"]["environment"]["ROADS_REMOTE"] == "true"
    assert services["caddy"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert all(v["target"] != "/data" for v in services["api"]["volumes"])


def test_full_plan_with_external_profiles(tmp_path, monkeypatch):
    from app.domain.models import Job, PlanRequest
    from app.routing import local_roads
    from app.services.planner import PlanningService
    seen = set()
    def respond(req):
        _, service, _, profile, coords = req.url.path.split("/")
        seen.add(profile)
        points = [list(map(float, p.split(","))) for p in coords.split(";")]
        if service == "table":
            origins = req.url.params["sources"].split(";")
            destinations = req.url.params["destinations"].split(";")
            return httpx.Response(200, json={"code": "Ok", "distances": [
                [0 if points[int(a)] == points[int(b)] else 1000 for b in destinations]
                for a in origins]})
        return httpx.Response(200, json={"code": "Ok", "routes": [
            {"geometry": {"coordinates": points}}]})
    async def immediate(client, url, params):
        return await client.get(url, params=params)
    monkeypatch.setattr(local_roads, "remote_get", immediate)
    monkeypatch.setattr(LocalRoadsRoutingProvider, "client", lambda self: httpx.AsyncClient(
        transport=httpx.MockTransport(respond)))
    day = datetime(2026, 9, 28, tzinfo=UTC)
    modes = ("car", "walk", "bicycle", "public_transport")
    req = PlanRequest(planning_time=day, engineers=[
        Engineer(id=mode, name=mode, start_location=A, transport_modes={mode},
                 skills={mode}, shift=TimeWindow(start=day, end=day+timedelta(hours=8)))
        for mode in modes], jobs=[Job(id=mode, title=mode, location=B,
                                     required_skills={mode}) for mode in modes])
    result = asyncio.run(PlanningService(settings(tmp_path, search_workers=1)).plan(req))
    assert {stop.job_id for route in result.routes for stop in route.stops} == set(modes)
    assert seen == {"car", "foot", "bicycle"}
