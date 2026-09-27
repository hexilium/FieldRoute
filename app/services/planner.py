from __future__ import annotations

from app.config import Settings
from app.domain.models import (
    DistrictMode,
    EmergencyReplanPolicy,
    OptimizationPolicy,
    PlanRequest,
    PlanResult,
    ServicePriorityPolicy,
    UrgencyPolicy,
    UrgentStartPolicy,
)
from app.routing.base import RoutingProvider, UnsupportedTravelProfile
from app.routing.cached import CachedRoutingProvider
from app.routing.haversine import HaversineRoutingProvider
from app.services.planning_settings import effective_settings, execution_snapshot, assumed_speeds
from app.routing.local_roads import LocalRoadsRoutingProvider
from app.routing.osrm import OsrmRoutingProvider
from app.services.map_data import build_map_data
from app.services.plan_diff import attach_previous_diff
from app.services.progress import current_progress
from app.solvers.base import Solver
from app.solvers.heuristic import HeuristicSolver
from app.solvers.insertion import BaselineSolver, InsertionSolver
from app.solvers.vroom import VroomSolver


class UnsupportedOptimizationPolicy(ValueError):
    pass


def build_travel_model(request: PlanRequest, settings: Settings) -> dict:
    road = settings.routing_backend == "osrm" or settings.solver_backend == "vroom"
    return {
        "kind": ("osm_fixed_speed_v1" if settings.routing_backend == "local_roads"
                 else "osrm_driving" if road else "haversine_by_transport_v1"),
        "engineers": {
            e.id: {"mode": e.travel_mode, "speed_kmh": (
                None if road else e.travel_speed_kmh or assumed_speeds(settings)[e.travel_mode]
            )}
            for e in request.engineers
        },
    }


class PlanningService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def for_request(self, request: PlanRequest) -> "PlanningService":
        resolved = effective_settings(self.settings, request)
        return self if resolved is self.settings else PlanningService(resolved)

    def routing_provider(self) -> RoutingProvider:
        if self.settings.routing_backend == "local_roads":
            return CachedRoutingProvider(LocalRoadsRoutingProvider(self.settings))
        routing = (
            OsrmRoutingProvider(self.settings.osrm_url)
            if self.settings.routing_backend == "osrm"
            else HaversineRoutingProvider(
                average_speed_kmh=self.settings.speed_car_kmh,
                road_factor=self.settings.haversine_road_factor,
                speeds_kmh=assumed_speeds(self.settings),
            )
        )
        return CachedRoutingProvider(routing)

    def _solver(self) -> Solver:
        if self.settings.solver_backend == "insertion":
            if self.settings.search_time_limit_ms is not None and self.settings.routing_backend == "osrm":
                raise UnsupportedOptimizationPolicy(
                    "Бюджет времени поиска поддерживается с Haversine и local_roads. "
                    "Для OSRM отключите search_time_limit_ms."
                )
            return InsertionSolver(
                self.routing_provider(), search_time_limit_ms=self.settings.search_time_limit_ms,
                workers=self.settings.search_workers,
                parallel_min_jobs=self.settings.search_parallel_min_jobs,
                search_strategy=self.settings.search_strategy,
                search_attempt_limit=self.settings.search_attempt_limit,
                search_slice_attempts=self.settings.search_slice_attempts,
            )
        if self.settings.solver_backend == "vroom":
            return VroomSolver(self.settings.vroom_url)

        solver_class = {
            "baseline": BaselineSolver,
            "heuristic": HeuristicSolver,
        }[self.settings.solver_backend]
        return solver_class(self.routing_provider())

    def _validate_request(self, request: PlanRequest) -> None:
        if request.district_mode == DistrictMode.strict and self.settings.solver_backend not in {"insertion", "baseline"}:
            raise UnsupportedOptimizationPolicy(
                "Расчёт по районам поддерживают insertion и baseline. "
                "Экспериментальный алгоритм не должен игнорировать запрет выездов."
            )
        if self.settings.routing_backend == "local_roads" and self.settings.solver_backend not in {
            "insertion", "baseline",
        }:
            raise UnsupportedOptimizationPolicy(
                "Локальные дорожные профили поддерживаются алгоритмами insertion и baseline."
            )
        if self.settings.solver_backend not in {"insertion", "baseline"} and any(
            e.travel_mode != "car" or e.travel_speed_kmh is not None for e in request.engineers
        ):
            raise UnsupportedTravelProfile(
                "Профили времени движения поддерживаются алгоритмами insertion и baseline. "
                "Экспериментальный алгоритм поддерживает только автомобиль без заданной скорости."
            )
        extended_rules = (
            request.optimization_policy != OptimizationPolicy.staff_first
            or request.urgency_policy != UrgencyPolicy.urgent_first
            or request.urgent_start_policy != UrgentStartPolicy.after_primary
            or request.service_priority_policy != ServicePriorityPolicy.numeric
            or request.emergency_replan_policy != EmergencyReplanPolicy.respect_freeze
            or any(j.window_semantics != "start" for j in request.jobs)
        )
        if extended_rules and self.settings.solver_backend not in {"insertion", "baseline"}:
            raise UnsupportedOptimizationPolicy(
                "Выбранные цели, диспетчерский приоритет типов работ, правила аварийного "
                "перепланирования, раннее начало срочных и правила окон "
                "поддерживаются "
                "алгоритмами insertion и baseline. Экспериментальный алгоритм их не поддерживает."
            )

    def _decorate(self, request: PlanRequest, result: PlanResult) -> PlanResult:
        from app.services.districts import attach_district_summary

        attach_district_summary(request, result)
        result.diagnostics["calculation_settings"] = {
            "schema_version": 1, "execution": execution_snapshot(self.settings),
            "solver_backend": self.settings.solver_backend,
        }
        result.diagnostics["travel_model"] = build_travel_model(request, self.settings)
        result.map_data = build_map_data(
            request,
            local_roads=self.settings.routing_backend == "local_roads",
            road_distances=(
                self.settings.routing_backend == "osrm" or self.settings.solver_backend == "vroom"
            ),
        )
        return attach_previous_diff(request, result)

    async def plan(self, request: PlanRequest) -> PlanResult:
        return await self.for_request(request)._plan(request)

    async def _plan(self, request: PlanRequest) -> PlanResult:
        self._validate_request(request)
        progress = current_progress.get()
        if progress is not None:
            progress.begin(sum(j.status not in {"completed", "cancelled"} for j in request.jobs),
                           self.settings.solver_backend)
        result = await self._solver().solve(request)
        return self._decorate(request, result)

    async def plan_policies(
        self, request: PlanRequest, policies: tuple[OptimizationPolicy, ...],
    ) -> dict[OptimizationPolicy, PlanResult]:
        return await self.for_request(request)._plan_policies(request, policies)

    async def _plan_policies(
        self,
        request: PlanRequest,
        policies: tuple[OptimizationPolicy, ...],
    ) -> dict[OptimizationPolicy, PlanResult]:
        """Return several policy views of one shared insertion candidate search."""
        self._validate_request(request)
        policies = tuple(OptimizationPolicy(policy) for policy in policies)
        if not policies or len(policies) != len(set(policies)):
            raise ValueError("policies must contain unique optimization policies")
        if (self.settings.solver_backend != "insertion"
                or self.settings.search_time_limit_ms is not None
                or self.settings.search_strategy == "adaptive"):
            return {
                policy: await self.plan(request.model_copy(update={"optimization_policy": policy}))
                for policy in policies
            }
        progress = current_progress.get()
        if progress is not None:
            progress.begin(
                sum(j.status not in {"completed", "cancelled"} for j in request.jobs),
                self.settings.solver_backend,
            )
        solver = self._solver()
        assert isinstance(solver, InsertionSolver)
        results = await solver.solve_policies(request, policies)
        return {
            policy: self._decorate(
                request.model_copy(update={"optimization_policy": policy}), result,
            )
            for policy, result in results.items()
        }
