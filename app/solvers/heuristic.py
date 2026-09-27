from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import perf_counter

from app.domain.models import (
    CandidateEvaluation,
    Engineer,
    EngineerRoute,
    Job,
    JobDecision,
    JobStatus,
    PlannedStop,
    PlanRequest,
    PlanResult,
    UnassignedJob,
)
from app.routing.base import RoutingProvider
from app.services.explainer import assignment_explanation
from app.services.metrics import build_metrics, previous_assignment_map, previous_stop_map
from app.services.stability import forced_assignment
from app.solvers.base import Solver


@dataclass
class RouteState:
    engineer: Engineer
    location: object
    moment: datetime
    route: EngineerRoute


class HeuristicSolver(Solver):
    """Fast local fallback, dynamic-replan engine and transparent baseline.

    It is intentionally simple enough to explain during a pitch, but already respects
    skills, equipment, transport, shifts, time windows, locked assignments and a freeze
    horizon around the current time.
    """

    def __init__(self, routing: RoutingProvider) -> None:
        self.routing = routing

    @staticmethod
    def _eligible(job: Job, engineer: Engineer) -> list[str]:
        reasons: list[str] = []
        if not engineer.available:
            reasons.append("engineer_unavailable")
        if not job.required_skills.issubset(engineer.skills):
            reasons.append("skills")
        if not job.required_equipment.issubset(engineer.equipment):
            reasons.append("equipment")
        if job.required_transport and job.required_transport not in engineer.transport_modes:
            reasons.append("transport")
        return reasons

    @staticmethod
    def _window_for(job: Job, arrival: datetime) -> tuple[datetime, datetime] | None:
        service_delta = timedelta(minutes=job.service_minutes)
        if not job.time_windows:
            return arrival, arrival + service_delta
        for window in sorted(job.time_windows, key=lambda w: w.end):
            service_start = max(arrival, window.start)
            # The common VRPTW convention is "service must start inside the window".
            if service_start <= window.end:
                return service_start, service_start + service_delta
        return None

    async def solve(self, request: PlanRequest) -> PlanResult:
        if request.district_mode == "strict":
            raise ValueError("Строгие районы поддерживают только insertion и baseline")
        started = perf_counter()
        states: dict[str, RouteState] = {}
        for engineer in request.engineers:
            start_moment = max(request.planning_time, engineer.shift.start)
            if engineer.available_from is not None:
                start_moment = max(start_moment, engineer.available_from)
            states[engineer.id] = RouteState(
                engineer=engineer,
                location=engineer.current_location or engineer.start_location,
                moment=start_moment,
                route=EngineerRoute(
                    engineer_id=engineer.id,
                    engineer_name=engineer.name,
                    stops=[],
                ),
            )

        active_jobs = [
            j for j in request.jobs if j.status not in {JobStatus.completed, JobStatus.cancelled}
        ]
        previous = previous_assignment_map(request.previous_plan)
        old_stops = previous_stop_map(request.previous_plan)

        forced: dict[str, tuple[str | None, bool, str | None]] = {
            job.id: forced_assignment(job, request, previous, old_stops)
            for job in active_jobs
        }

        def job_order(job: Job) -> tuple:
            _, frozen, _ = forced[job.id]
            old = old_stops.get(job.id)
            old_time = old.service_start if old else datetime.max.replace(tzinfo=UTC)
            window_end = min(
                (w.end for w in job.time_windows),
                default=datetime.max.replace(tzinfo=UTC),
            )
            return (0 if frozen else 1, old_time if frozen else window_end, -job.priority, job.id)

        active_jobs.sort(key=job_order)

        unassigned: list[UnassignedJob] = []
        decisions: list[JobDecision] = []
        new_assignment: dict[str, str] = {}
        frozen_count = 0

        for job in active_jobs:
            forced_engineer, is_frozen, freeze_reason = forced[job.id]
            candidates: list[
                tuple[float, RouteState, float, float, datetime, datetime, list[str]]
            ] = []
            evaluations: list[CandidateEvaluation] = []
            eligibility_failures: set[str] = set()

            for state in states.values():
                reasons: list[str] = []
                if forced_engineer and state.engineer.id != forced_engineer:
                    reasons.append("frozen_to_other_engineer")
                if (state.engineer.max_jobs is not None
                        and len(state.route.stops) >= state.engineer.max_jobs):
                    reasons.append("max_jobs")
                reasons.extend(self._eligible(job, state.engineer))

                if reasons:
                    eligibility_failures.update(reasons)
                    evaluations.append(
                        CandidateEvaluation(
                            engineer_id=state.engineer.id,
                            engineer_name=state.engineer.name,
                            feasible=False,
                            reasons=sorted(set(reasons)),
                        )
                    )
                    continue

                distance_km, travel_minutes = await self.routing.distance_time(
                    state.location, job.location
                )
                arrival = state.moment + timedelta(minutes=travel_minutes)
                service = self._window_for(job, arrival)
                if service is None:
                    eligibility_failures.add("time_window")
                    evaluations.append(
                        CandidateEvaluation(
                            engineer_id=state.engineer.id,
                            engineer_name=state.engineer.name,
                            feasible=False,
                            travel_minutes=round(travel_minutes, 1),
                            distance_km=round(distance_km, 2),
                            reasons=["time_window"],
                        )
                    )
                    continue

                service_start, departure = service
                if departure > state.engineer.shift.end:
                    eligibility_failures.add("shift_end")
                    evaluations.append(
                        CandidateEvaluation(
                            engineer_id=state.engineer.id,
                            engineer_name=state.engineer.name,
                            feasible=False,
                            travel_minutes=round(travel_minutes, 1),
                            distance_km=round(distance_km, 2),
                            service_start=service_start,
                            reasons=["shift_end"],
                        )
                    )
                    continue

                waiting = max(0.0, (service_start - arrival).total_seconds() / 60.0)
                churn_penalty = 0.0
                old_engineer = previous.get(job.id)
                if old_engineer and old_engineer != state.engineer.id:
                    churn_penalty = request.weights.plan_churn

                schedule_shift_penalty = 0.0
                old_stop = old_stops.get(job.id)
                if old_stop is not None:
                    shift_minutes = abs(
                        (service_start - old_stop.service_start).total_seconds()
                    ) / 60.0
                    schedule_shift_penalty = shift_minutes * request.weights.schedule_shift

                sla_penalty = 0.0
                candidate_reasons = ["hard_constraints_ok"]
                if job.sla_deadline and service_start > job.sla_deadline:
                    late = (service_start - job.sla_deadline).total_seconds() / 60.0
                    sla_penalty = request.weights.sla_violation + late
                    candidate_reasons.append(f"sla_late:{late:.0f}m")
                elif job.sla_deadline:
                    candidate_reasons.append("sla_ok")

                score = (
                    travel_minutes * request.weights.travel_minutes
                    + distance_km * request.weights.distance_km
                    + waiting * 0.15
                    + churn_penalty
                    + schedule_shift_penalty
                    + sla_penalty
                    - job.priority * 0.05
                )
                candidates.append(
                    (
                        score,
                        state,
                        distance_km,
                        travel_minutes,
                        service_start,
                        departure,
                        candidate_reasons,
                    )
                )
                evaluations.append(
                    CandidateEvaluation(
                        engineer_id=state.engineer.id,
                        engineer_name=state.engineer.name,
                        feasible=True,
                        score=round(score, 2),
                        travel_minutes=round(travel_minutes, 1),
                        distance_km=round(distance_km, 2),
                        service_start=service_start,
                        reasons=candidate_reasons,
                    )
                )

            if not candidates:
                codes = sorted(eligibility_failures) or ["no_engineer"]
                unassigned.append(
                    UnassignedJob(
                        job_id=job.id,
                        reason_codes=codes,
                        explanation="Нет допустимого назначения при текущих ограничениях: "
                        + ", ".join(codes),
                    )
                )
                decisions.append(
                    JobDecision(
                        job_id=job.id,
                        chosen_engineer_id=None,
                        frozen=is_frozen,
                        candidates=evaluations,
                    )
                )
                continue

            score, state, km, travel_min, service_start, departure, _ = min(
                candidates, key=lambda x: x[0]
            )
            arrival = state.moment + timedelta(minutes=travel_min)
            explanation = assignment_explanation(job, state.engineer, travel_min)
            if is_frozen:
                frozen_count += 1
                if freeze_reason == "freeze_horizon":
                    explanation.insert(0, "назначение сохранено: заявка попала в freeze horizon")
                else:
                    explanation.insert(0, "назначение закреплено и не меняется при перепланировании")
            if job.sla_deadline:
                if service_start <= job.sla_deadline:
                    explanation.append("SLA по времени прибытия соблюдается")
                else:
                    late = (service_start - job.sla_deadline).total_seconds() / 60.0
                    explanation.append(f"есть риск нарушения SLA примерно на {late:.0f} мин")

            stop = PlannedStop(
                job_id=job.id,
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
            state.route.stops.append(stop)
            state.route.total_travel_minutes += travel_min
            state.route.total_distance_km += km
            state.route.work_minutes = max(
                0.0,
                (
                    departure - max(request.planning_time, state.engineer.shift.start)
                ).total_seconds()
                / 60.0,
            )
            state.location = job.location
            state.moment = departure
            new_assignment[job.id] = state.engineer.id
            decisions.append(
                JobDecision(
                    job_id=job.id,
                    chosen_engineer_id=state.engineer.id,
                    frozen=is_frozen,
                    candidates=evaluations,
                )
            )

        routes = [s.route for s in states.values()]
        for state in states.values():
            origin = state.engineer.current_location or state.engineer.start_location
            points = [origin, *[stop.location for stop in state.route.stops]]
            state.route.geometry = await self.routing.route_geometry(points) if len(points) > 1 else points

        changed = sum(
            1
            for job_id, engineer_id in new_assignment.items()
            if job_id in previous and previous[job_id] != engineer_id
        )
        solve_time_ms = (perf_counter() - started) * 1000.0
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
        routing_stats = getattr(self.routing, "stats", None)
        return PlanResult(
            generated_at=datetime.now(UTC),
            solver="local-heuristic-v2",
            routes=routes,
            unassigned=unassigned,
            metrics=metrics,
            decisions=decisions,
            diagnostics={
                "note": "Transparent local baseline and dynamic-replanning fallback.",
                "objective_order": [
                    "hard constraints / frozen assignments",
                    "SLA and priority",
                    "plan stability",
                    "travel time",
                    "distance",
                    "schedule shift",
                ],
                "routing_cache": routing_stats if isinstance(routing_stats, dict) else None,
            },
        )
