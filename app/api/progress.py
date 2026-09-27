"""Opt-in NDJSON transport. The ordinary JSON API remains unchanged."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError

from app.api.events import replan_event
from app.api.routes import (
    SettingsDependency,
    compare_baseline,
    compare_districts,
    compare_policies,
    compare_variants,
    demo_replanning,
    plan,
)
from app.domain.events import EventRequest
from app.domain.models import (
    OptimizationPolicy,
    PlanRequest,
    ReplanningProfile,
    UrgencyPolicy,
    UrgentStartPolicy,
    VariantComparisonRequest,
)
from app.routing.base import UnsupportedTravelProfile
from app.routing.local_roads import RoutingCoverageError, RoutingUnavailable
from app.services.planner import UnsupportedOptimizationPolicy
from app.services.progress import PlanningProgress, current_progress

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)


def stream_planning(operation: Callable[[], Awaitable[BaseModel]]) -> StreamingResponse:
    async def events():
        progress = PlanningProgress()

        async def run():
            token = current_progress.set(progress)
            try:
                result = await operation()
                message = {"type": "result", "data": result.model_dump(mode="json")}
            except ValidationError as exc:
                message = {"type": "error", "detail": "; ".join(e["msg"] for e in exc.errors()[:5])}
            except HTTPException as exc:
                message = {"type": "error", "detail": exc.detail}
            except (UnsupportedOptimizationPolicy, UnsupportedTravelProfile,
                    RoutingCoverageError, RoutingUnavailable) as exc:
                message = {"type": "error", "detail": str(exc)}
            except Exception:
                logger.exception("Streaming planning failed")
                message = {"type": "error", "detail": "Не удалось завершить расчёт. Проверьте журнал сервера."}
            finally:
                current_progress.reset(token)
            # Terminal messages are never coalesced or dropped.
            await progress.queue.put(message)

        task = asyncio.create_task(run())
        try:
            yield json.dumps(progress.snapshot(), ensure_ascii=False) + "\n"
            while True:
                try:
                    message = await asyncio.wait_for(progress.queue.get(), timeout=1)
                except TimeoutError:
                    message = progress.snapshot()
                yield json.dumps(message, ensure_ascii=False) + "\n"
                if message["type"] != "progress":
                    break
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    return StreamingResponse(events(), media_type="application/x-ndjson", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
    })


@router.post("/plan/stream")
async def plan_stream(payload: PlanRequest, settings: SettingsDependency):
    return stream_planning(lambda: plan(payload, settings))


@router.post("/compare/stream")
async def compare_stream(payload: PlanRequest, settings: SettingsDependency):
    return stream_planning(lambda: compare_baseline(payload, settings))


@router.post("/policies/compare/stream")
async def policies_stream(payload: PlanRequest, settings: SettingsDependency):
    return stream_planning(lambda: compare_policies(payload, settings))


@router.post("/variants/compare/stream")
async def variants_stream(
    payload: VariantComparisonRequest,
    settings: SettingsDependency,
):
    return stream_planning(lambda: compare_variants(payload, settings))


@router.post("/events/replan/stream")
async def event_stream(payload: EventRequest, settings: SettingsDependency):
    return stream_planning(lambda: replan_event(payload, settings))


@router.get("/demo/replanning/stream")
async def incident_stream(
    settings: SettingsDependency,
    optimization_policy: OptimizationPolicy = OptimizationPolicy.staff_first,
    urgency_policy: UrgencyPolicy = UrgencyPolicy.urgent_first,
    urgent_start_policy: UrgentStartPolicy = UrgentStartPolicy.after_primary,
    before_variant: ReplanningProfile = ReplanningProfile.full,
    after_variant: ReplanningProfile = ReplanningProfile.conservative,
):
    return stream_planning(lambda: demo_replanning(
        settings, optimization_policy, urgency_policy, urgent_start_policy,
        before_variant, after_variant,
    ))


@router.post("/districts/compare/stream")
async def districts_stream(payload: PlanRequest, settings: SettingsDependency):
    return stream_planning(lambda: compare_districts(payload, settings))
