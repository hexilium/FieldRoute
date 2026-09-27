from __future__ import annotations

from datetime import UTC, datetime, timedelta
from time import perf_counter

import httpx

from app.domain.models import (
    EngineerRoute,
    JobStatus,
    PlannedStop,
    PlanRequest,
    PlanResult,
    UnassignedJob,
)
from app.services.metrics import build_metrics, previous_assignment_map, previous_stop_map
from app.services.stability import forced_assignment
from app.solvers.base import Solver


class VroomSolver(Solver):
    """Adapter for a fully local VROOM + OSRM stack.

    Near-term frozen/locked jobs are mapped to synthetic skills, so VROOM can keep hard
    assignments stable. Soft plan-churn and schedule-shift penalties remain the domain of a
    custom/metaheuristic solver if the final TЗ makes them important.
    """

    def __init__(self, base_url: str, timeout_seconds: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _payload(
        self, request: PlanRequest
    ) -> tuple[dict, dict[int, str], dict[int, str], int]:
        previous = previous_assignment_map(request.previous_plan)
        old_stops = previous_stop_map(request.previous_plan)
        active_jobs = [
            j for j in request.jobs if j.status not in {JobStatus.completed, JobStatus.cancelled}
        ]

        forced: dict[str, str] = {}
        frozen_count = 0
        for job in active_jobs:
            engineer_id, is_frozen, _ = forced_assignment(job, request, previous, old_stops)
            if engineer_id and is_frozen:
                forced[job.id] = engineer_id
                frozen_count += 1

        # VROOM has one integer skill namespace. Keep domain dimensions separate,
        # including literal user codes such as "transport:car" or "lock:job-1".
        capability_names: set[tuple[str, str]] = set()
        for engineer in request.engineers:
            capability_names.update(("skill", name) for name in engineer.skills)
            capability_names.update(("equipment", name) for name in engineer.equipment)
            capability_names.update(("transport", mode) for mode in engineer.transport_modes)
        for job in active_jobs:
            capability_names.update(("skill", name) for name in job.required_skills)
            capability_names.update(("equipment", name) for name in job.required_equipment)
            if job.required_transport:
                capability_names.add(("transport", job.required_transport))
            if job.id in forced:
                capability_names.add(("lock", job.id))

        capability_id = {name: i + 1 for i, name in enumerate(sorted(capability_names))}

        vehicle_ids: dict[int, str] = {}
        vehicles = []
        for engineer in request.engineers:
            if not engineer.available:
                continue
            start_time = max(request.planning_time, engineer.shift.start)
            if engineer.available_from is not None:
                start_time = max(start_time, engineer.available_from)
            if start_time >= engineer.shift.end:
                continue

            vehicle_id = len(vehicles)
            vehicle_ids[vehicle_id] = engineer.id
            start = engineer.current_location or engineer.start_location
            end = engineer.end_location or engineer.start_location
            capabilities = {("skill", name) for name in engineer.skills}
            capabilities |= {("equipment", name) for name in engineer.equipment}
            capabilities |= {("transport", mode) for mode in engineer.transport_modes}
            capabilities |= {
                ("lock", job_id) for job_id, engineer_id in forced.items() if engineer_id == engineer.id
            }
            vehicles.append(
                {
                    "id": vehicle_id,
                    "description": engineer.id,
                    "start": [start.lon, start.lat],
                    "end": [end.lon, end.lat],
                    "skills": [capability_id[x] for x in sorted(capabilities)],
                    "time_window": [int(start_time.timestamp()), int(engineer.shift.end.timestamp())],
                    **({"max_tasks": engineer.max_jobs} if engineer.max_jobs is not None else {}),
                }
            )

        job_ids: dict[int, str] = {}
        jobs = []
        for job in active_jobs:
            job_id = len(jobs)
            job_ids[job_id] = job.id
            required = {("skill", name) for name in job.required_skills}
            required |= {("equipment", name) for name in job.required_equipment}
            if job.required_transport:
                required.add(("transport", job.required_transport))
            if job.id in forced:
                required.add(("lock", job.id))
            item = {
                "id": job_id,
                "description": job.id,
                "location": [job.location.lon, job.location.lat],
                "service": job.service_minutes * 60,
                "priority": min(100, max(0, job.priority)),
                "skills": [capability_id[x] for x in sorted(required)],
            }
            if job.time_windows:
                item["time_windows"] = [
                    [int(w.start.timestamp()), int(w.end.timestamp())] for w in job.time_windows
                ]
            jobs.append(item)

        return {"vehicles": vehicles, "jobs": jobs, "options": {"g": True}}, vehicle_ids, job_ids, frozen_count

    async def solve(self, request: PlanRequest) -> PlanResult:
        if request.district_mode == "strict":
            raise ValueError("Строгие районы поддерживают только insertion и baseline")
        started = perf_counter()
        payload, vehicle_ids, job_ids, frozen_count = self._payload(request)
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(self.base_url, json=payload)
            response.raise_for_status()
            raw = response.json()

        if raw.get("code", 0) != 0:
            raise RuntimeError(f"VROOM error: {raw.get('error', 'unknown error')}")

        engineers = {e.id: e for e in request.engineers}
        jobs = {j.id: j for j in request.jobs}
        tz = request.planning_time.tzinfo or UTC
        routes: list[EngineerRoute] = []
        new_assignment: dict[str, str] = {}

        for raw_route in raw.get("routes", []):
            engineer_id = vehicle_ids[raw_route["vehicle"]]
            engineer = engineers[engineer_id]
            stops: list[PlannedStop] = []
            previous_duration_s = 0
            previous_distance_m = 0

            for step in raw_route.get("steps", []):
                duration_s = int(step.get("duration", previous_duration_s))
                distance_m = int(step.get("distance", previous_distance_m))
                if step.get("type") != "job":
                    previous_duration_s = duration_s
                    previous_distance_m = distance_m
                    continue

                job_id = job_ids[int(step["id"])]
                job = jobs[job_id]
                arrival_ts = int(step["arrival"])
                wait_s = int(step.get("waiting_time", 0))
                service_s = int(step.get("service", job.service_minutes * 60))
                arrival = datetime.fromtimestamp(arrival_ts, tz=tz)
                service_start = arrival + timedelta(seconds=wait_s)
                departure = service_start + timedelta(seconds=service_s)
                travel_min = max(0, duration_s - previous_duration_s) / 60.0
                km = max(0, distance_m - previous_distance_m) / 1000.0

                explanation = [
                    "назначение прошло жесткие ограничения VROOM",
                    f"добавочный дорожный переезд около {travel_min:.0f} мин",
                ]
                if job.required_skills:
                    explanation.append("квалификация инженера покрывает требования заявки")
                if job.required_equipment:
                    explanation.append("у инженера есть необходимое оборудование")
                if job.time_windows:
                    explanation.append("визит помещается во временное окно")
                _, is_frozen, reason = forced_assignment(job, request)
                if is_frozen:
                    explanation.insert(
                        0,
                        "назначение сохранено из опубликованного плана"
                        if reason == "freeze_horizon"
                        else "назначение закреплено",
                    )
                if job.sla_deadline:
                    explanation.append(
                        "SLA соблюдается"
                        if service_start <= job.sla_deadline
                        else "есть риск нарушения SLA"
                    )

                stops.append(
                    PlannedStop(
                        job_id=job_id,
                        title=job.title,
                        location=job.location,
                        arrival=arrival,
                        service_start=service_start,
                        departure=departure,
                        travel_minutes_from_previous=round(travel_min, 1),
                        distance_km_from_previous=round(km, 2),
                        explanation=explanation,
                        frozen=is_frozen,
                    )
                )
                new_assignment[job_id] = engineer_id
                previous_duration_s = duration_s
                previous_distance_m = distance_m

            route_end = stops[-1].departure if stops else max(request.planning_time, engineer.shift.start)
            overtime = max(0.0, (route_end - engineer.shift.end).total_seconds() / 60.0)
            routes.append(
                EngineerRoute(
                    engineer_id=engineer_id,
                    engineer_name=engineer.name,
                    stops=stops,
                    total_travel_minutes=round(float(raw_route.get("duration", 0)) / 60.0, 1),
                    total_distance_km=round(float(raw_route.get("distance", 0)) / 1000.0, 2),
                    work_minutes=max(
                        0.0,
                        (
                            route_end - max(request.planning_time, engineer.shift.start)
                        ).total_seconds()
                        / 60.0,
                    ),
                    overtime_minutes=overtime,
                )
            )

        present = {r.engineer_id for r in routes}
        for engineer in request.engineers:
            if engineer.id not in present:
                routes.append(
                    EngineerRoute(engineer_id=engineer.id, engineer_name=engineer.name, stops=[])
                )

        unassigned: list[UnassignedJob] = []
        for item in raw.get("unassigned", []):
            job_id = job_ids[int(item["id"])]
            unassigned.append(
                UnassignedJob(
                    job_id=job_id,
                    reason_codes=["solver_unassigned"],
                    explanation="VROOM не нашел допустимое назначение при текущем наборе ограничений.",
                )
            )

        previous = previous_assignment_map(request.previous_plan)
        changed = sum(
            1
            for job_id, engineer_id in new_assignment.items()
            if job_id in previous and previous[job_id] != engineer_id
        )
        active_jobs = [
            j for j in request.jobs if j.status not in {JobStatus.completed, JobStatus.cancelled}
        ]
        solve_time_ms = (perf_counter() - started) * 1000
        metrics = build_metrics(
            routes,
            len(active_jobs),
            len(unassigned),
            changed,
            jobs=active_jobs,
            planning_time=request.planning_time,
            previous_plan=request.previous_plan,
            frozen_jobs=frozen_count,
            solve_time_ms=solve_time_ms,
        )
        return PlanResult(
            generated_at=datetime.now(UTC),
            solver="vroom-local",
            routes=sorted(routes, key=lambda r: r.engineer_id),
            unassigned=unassigned,
            metrics=metrics,
            diagnostics={
                "vroom_summary": raw.get("summary", {}),
                "note": (
                    "VROOM handles hard freeze/locks through synthetic skills. "
                    "Soft plan-churn tuning requires a custom/metaheuristic layer."
                ),
            },
        )
