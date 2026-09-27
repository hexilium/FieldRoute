"""Independent strict district solves, followed by a full independent validation.

Configured attempt/time allowances are shared, not multiplied by district count.
Each district with future work and staff receives an equal deterministic slice;
unused slices are not transferred. Time is cooperative search time, not HTTP time.
"""
from __future__ import annotations

import asyncio
from copy import copy, deepcopy
from datetime import UTC, datetime
from time import perf_counter

from app.domain.models import JobStatus, OptimizationPolicy, PlanRequest, PlanResult
from app.services.districts import attach_district_summary
from app.services.metrics import build_metrics, previous_assignment_map
from app.services.plan_diff import attach_previous_diff
from app.services.progress import current_progress
from app.services.validation import validate_plan


def district_requests(request: PlanRequest) -> list[tuple[str, PlanRequest]]:
    active = [j for j in request.jobs if j.status not in {JobStatus.completed, JobStatus.cancelled}]
    district_ids = sorted({e.district_id for e in request.engineers} | {j.district_id for j in active})
    result = []
    for district in district_ids:
        engineers = [e for e in request.engineers if e.district_id == district]
        ids = {e.id for e in engineers}
        jobs = [j.model_copy(deep=True) for j in active if j.district_id == district]
        for job in jobs:
            if job.assigned_engineer_id not in ids:
                # The complete request was validated before partitioning. Thus an
                # outside owner here can only be a non-binding hint on a pending job.
                job.assigned_engineer_id = None
        child = request.model_copy(update={"engineers": engineers, "jobs": jobs})
        # Keep the previous snapshot: it is a reference, not a pool of future jobs.
        # This preserves churn accounting when switching from unrestricted mode.
        result.append((district, PlanRequest.model_validate(child.model_dump())))
    return result


def integer_shares(limit: int, ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    whole, remainder = divmod(limit, len(ids))
    return {district: whole + (i < remainder) for i, district in enumerate(ids)}


async def solve_by_district(solver, request: PlanRequest, policies: tuple[OptimizationPolicy, ...]):
    started = perf_counter()
    parts = district_requests(request)
    if not parts:
        # The flat empty plan is still independently checked by its normal solver.
        results = await solver._solve_flat_policies(request, policies)
        for result in results.values():
            attach_district_summary(request, result)
            result.diagnostics["district_search"] = {"strategy": "independent_districts", "parts": []}
        return results
    funded = [d for d, r in parts if r.engineers and any(j.status != JobStatus.in_progress for j in r.jobs)]
    adaptive = getattr(solver, "search_strategy", None) == "adaptive"
    total_attempts = getattr(solver, "search_attempt_limit", None) if adaptive else None
    shares = integer_shares(total_attempts, funded) if adaptive else {}
    total_ms = getattr(solver, "search_time_limit_ms", None)
    ms_share = total_ms / len(funded) if total_ms is not None and funded else 0.0
    combined = {p: [] for p in policies}
    progress = current_progress.get()
    for number, (district, child_request) in enumerate(parts, 1):
        await asyncio.sleep(0)  # Cancellation is not swallowed between partitions.
        if progress is not None:
            progress.update(
                force=True, phase="routing", stage="", district_id=district,
                district_number=number, district_count=len(parts),
                total_jobs=len(child_request.jobs), processed_jobs=0, best_assigned=0,
                schedule_attempts=0, completed_candidates=0,
            )
        child_solver = copy(solver)
        if adaptive:
            child_solver.search_attempt_limit = shares.get(district, 0)
        if total_ms is not None:
            child_solver.search_time_limit_ms = ms_share if district in funded else 0.0
        # For explicit multi-policy timed calls, preserve independent per-policy budgets.
        if total_ms is not None and len(policies) > 1:
            results = {}
            for p in policies:
                results.update(await child_solver._solve_flat_policies(
                    child_request.model_copy(update={"optimization_policy": p}), (p,),
                ))
        else:
            results = await child_solver._solve_flat_policies(child_request, policies)
        for policy, result in results.items():
            if not child_request.engineers:
                for item in result.unassigned:
                    item.reason_codes = ["district_no_engineer"]
                    item.explanation = f"В районе {district} нет инженеров. Выезды из других районов запрещены."
            combined[policy].append((district, result, {
                "attempt_limit": shares.get(district, 0) if adaptive else None,
                "time_limit_ms": (ms_share if district in funded else 0.0) if total_ms is not None else None,
            }))
    if progress is not None:
        progress.update(force=True, phase="validation", district_id=None,
                        district_number=len(parts), district_count=len(parts), total_jobs=sum(len(r.jobs) for _, r in parts))
    results = {}
    active = [j for j in request.jobs if j.status not in {JobStatus.completed, JobStatus.cancelled}]
    old = previous_assignment_map(request.previous_plan)
    for policy in policies:
        pieces = combined[policy]
        route_map = {r.engineer_id: r for _, plan, _ in pieces for r in plan.routes}
        routes = [route_map[e.id] for e in request.engineers]
        unassigned_map = {j.job_id: j for _, plan, _ in pieces for j in plan.unassigned}
        unassigned = [unassigned_map[j.id] for j in active if j.id in unassigned_map]
        decisions_map = {j.job_id: j for _, plan, _ in pieces for j in plan.decisions}
        decisions = [decisions_map[j.id] for j in active if j.id in decisions_map]
        changed = sum(s.job_id in old and old[s.job_id] != r.engineer_id for r in routes for s in r.stops)
        metrics = build_metrics(
            routes, len(active), len(unassigned), changed, jobs=active,
            planning_time=request.planning_time, previous_plan=request.previous_plan,
            frozen_jobs=sum(s.frozen for r in routes for s in r.stops),
        )
        diagnostics = {
            "optimization_policy": policy.value, "urgency_policy": request.urgency_policy.value,
            "urgent_start_policy": request.urgent_start_policy.value,
            "optimality_proven": False,
            "distance_scope": "remaining" if request.previous_plan is not None else "whole_shift",
            "district_search": {
                "strategy": "independent_districts", "execution": "sequential_districts",
                "budget_scope": "per_request_per_policy; equal_slices_no_redistribution",
                "attempt_limit": total_attempts, "time_limit_ms": total_ms,
                "parts": [{"district_id": d, **limits, "metrics": plan.metrics.model_dump(mode="json"),
                           "diagnostics": deepcopy(plan.diagnostics)} for d, plan, limits in pieces],
            },
            "route_evaluations": sum(plan.diagnostics.get("route_evaluations", 0) for _, plan, _ in pieces),
            "accepted_moves": sum(plan.diagnostics.get("accepted_moves", 0) for _, plan, _ in pieces),
            "execution": {"shared_candidate_pool": len(policies) > 1 and not adaptive and total_ms is None,
                          "result_policies": [p.value for p in policies]},
        }
        # Do not invent a single candidate-pool winner: each district selected its own.
        if any(plan.diagnostics.get("search_budget", {}).get("stop_reason") == "time_limit" for _, plan, _ in pieces):
            diagnostics["search_budget"] = {
                "stop_reason": "time_limit", "scope": "district_search_slices; not_http_deadline",
                "limit_ms": total_ms,
            }
        result = PlanResult(generated_at=datetime.now(UTC), solver="districts-v22/" + pieces[0][1].solver,
                            routes=routes, unassigned=unassigned, decisions=decisions,
                            metrics=metrics, diagnostics=diagnostics)
        policy_request = request.model_copy(update={"optimization_policy": policy})
        attach_previous_diff(policy_request, result)
        errors = await validate_plan(policy_request, result, solver.routing)
        if errors:
            raise RuntimeError("District merge validation failed: " + ", ".join(errors[:12]))
        # Reconstruct the verified published schedule for a global objective
        # diagnostic. This is scoring, not another search and not a global pool.
        from app.solvers.insertion import Search

        scoring = Search(policy_request, solver.routing)
        schedules = await scoring.evaluator.published_routes(result)
        result.diagnostics["objective_order"] = scoring.objective_order()
        result.diagnostics["objective_value"] = list(scoring.key(schedules))
        result.diagnostics["selected_candidate"] = "district_aggregate"
        result.diagnostics["validation"] = "passed"
        attach_district_summary(policy_request, result)
        results[policy] = result
    duration = round((perf_counter()-started)*1000, 1)
    for result in results.values():
        result.metrics.solve_time_ms = duration
    return results
