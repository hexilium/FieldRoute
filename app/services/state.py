from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.services.planning_settings import effective_settings
from app.domain.events import (
    CancelJobEvent,
    EngineerDelayEvent,
    EngineerUnavailableEvent,
    ManualAssignmentEvent,
    NewJobEvent,
    UrgentJobEvent,
)
from app.domain.models import Job, JobStatus
from app.domain.state import SavedState
from app.services.map_data import build_map_data
from app.services.metrics import build_labor_metrics, build_workload_metrics
from app.services.plan_diff import attach_previous_diff
from app.services.planner import PlanningService, build_travel_model
from app.services.validation import validate_plan


class InvalidState(ValueError):
    pass


class DistanceModelMismatch(InvalidState):
    def __init__(self, saved_distance_model: str, current_distance_model: str) -> None:
        super().__init__("Модель расстояний сохранённого плана отличается от настроек сервера.")
        self.saved_distance_model = saved_distance_model
        self.current_distance_model = current_distance_model


def validate_history(state: SavedState) -> None:
    jobs = {job.id: job for job in state.request.jobs}
    engineers = {engineer.id: engineer for engineer in state.request.engineers}
    previous_time = state.origin.request.planning_time if state.origin else None
    seen: set[tuple[str, str]] = set()
    added: dict[str, Job] = {}
    delayed_until: dict[str, datetime] = {}
    manual_assignments: dict[str, str] = {}
    # A manual action edits the plan at its current clock time; it never advances
    # execution. Track only statuses established by the origin or the journal,
    # because the final snapshot may include a later cancellation/completion.
    unavailable_jobs = {
        job.id for job in (state.origin.request.jobs if state.origin else [])
        if job.status in {JobStatus.in_progress, JobStatus.completed, JobStatus.cancelled}
    }
    unavailable_engineers = {
        engineer.id for engineer in (state.origin.request.engineers if state.origin else [])
        if not engineer.available
    }
    known_jobs = set(jobs) - {
        event.job.id
        for event in state.events
        if isinstance(event, (UrgentJobEvent, NewJobEvent))
    }
    for event in state.events:
        if event.time > state.request.planning_time or (
            previous_time is not None and event.time < previous_time
        ):
            raise InvalidState("Время в журнале событий не согласовано с состоянием.")
        if isinstance(event, ManualAssignmentEvent) and (
            previous_time is not None and event.time != previous_time
        ):
            raise InvalidState("Ручное назначение не может изменять время текущего плана.")
        previous_time = event.time
        if isinstance(event, (UrgentJobEvent, NewJobEvent)):
            target = event.job.id
            if target not in jobs:
                raise InvalidState("Добавленная заявка из журнала отсутствует в состоянии.")
            if target in known_jobs:
                raise InvalidState("Журнал повторно добавляет заявку с тем же ID.")
            added_job = Job.model_validate(event.job.model_dump())
            if isinstance(event, UrgentJobEvent):
                added_job.priority = 100
                added_job.metadata = dict(added_job.metadata) | {
                    "dispatch_work_type": "emergency",
                    "route_change_trigger": "emergency_event",
                    "appeared_at": event.time.isoformat(),
                }
            added[target] = added_job
            known_jobs.add(target)
        elif isinstance(event, CancelJobEvent):
            target = event.job_id
            if target not in known_jobs or jobs[target].status != JobStatus.cancelled:
                raise InvalidState("Отмена в журнале не соответствует статусу заявки.")
            unavailable_jobs.add(target)
        elif isinstance(event, EngineerUnavailableEvent):
            target = event.engineer_id
            if target not in engineers or engineers[target].available:
                raise InvalidState("Недоступность в журнале не соответствует состоянию инженера.")
            unavailable_engineers.add(target)
        elif isinstance(event, EngineerDelayEvent):
            target = event.engineer_id
            if target not in engineers or target in unavailable_engineers:
                raise InvalidState("Задержка в журнале ссылается на неизвестного или недоступного инженера.")
            until = event.time + timedelta(minutes=event.delay_minutes)
            delayed_until[target] = max(delayed_until.get(target, until), until)
            # Several delays of the same engineer are valid sequential events.
            continue
        elif isinstance(event, ManualAssignmentEvent):
            target = event.job_id
            if target not in known_jobs or event.engineer_id not in engineers:
                raise InvalidState("Ручное назначение ссылается на неизвестную заявку или инженера.")
            if target in unavailable_jobs:
                raise InvalidState("Нельзя вручную назначить начатую, завершённую или отменённую заявку.")
            if event.engineer_id in unavailable_engineers:
                raise InvalidState("Нельзя вручную назначить заявку недоступному инженеру.")
            manual_assignments[target] = event.engineer_id
            # Repeated changes, including A -> B -> A at the same planning time,
            # are separate dispatcher decisions rather than duplicated events.
            continue
        key = (event.type, target)
        if key in seen:
            raise InvalidState("Журнал содержит повторно применённое событие.")
        seen.add(key)
    if state.events and state.events[-1].time != state.request.planning_time:
        raise InvalidState("Последнее событие должно соответствовать времени текущего состояния.")
    for job_id, engineer_id in manual_assignments.items():
        job = jobs[job_id]
        if not job.locked or job.assigned_engineer_id != engineer_id:
            raise InvalidState("Ручное назначение в журнале не соответствует закреплению заявки.")
    for engineer_id, until in delayed_until.items():
        if (
            engineers[engineer_id].available_from is None
            or engineers[engineer_id].available_from < until
        ):
            raise InvalidState("Задержка в журнале не соответствует времени доступности инженера.")

    expected = dict(added)
    if state.origin:
        origin = state.origin.request
        if origin.planning_time > state.request.planning_time:
            raise InvalidState("Исходный набор находится позже текущего состояния.")
        if {e.id for e in origin.engineers} != engineers.keys():
            raise InvalidState("Состав инженеров не совпадает с исходным набором.")
        for engineer in origin.engineers:
            mutable = {"available", "available_from", "current_location"}
            if engineer.model_dump(exclude=mutable) != engineers[engineer.id].model_dump(
                exclude=mutable
            ):
                raise InvalidState("Характеристики инженера не совпадают с исходным набором.")
            if not engineer.available and engineers[engineer.id].available:
                raise InvalidState("Возврат доступности отсутствует в контракте событий.")
        for job in origin.jobs:
            if job.id in added:
                raise InvalidState("Добавленная заявка уже присутствует в исходном наборе.")
            expected[job.id] = job
        if expected.keys() != jobs.keys():
            raise InvalidState("Состав заявок не совпадает с исходным набором и журналом.")
    for jid, original in expected.items():
        mutable = {"status", "assigned_engineer_id"}
        if jid in manual_assignments:
            mutable.add("locked")
        if original.model_dump(exclude=mutable) != jobs[jid].model_dump(exclude=mutable):
            raise InvalidState("Параметры заявки не совпадают с исходным набором или событием.")
        if original.status in {JobStatus.cancelled, JobStatus.completed} and (
            jobs[jid].status != original.status
        ):
            raise InvalidState("Неактивная заявка исходного набора не может быть возобновлена.")
        if jid not in manual_assignments and original.assigned_engineer_id and (
            jobs[jid].assigned_engineer_id != original.assigned_engineer_id
        ):
            raise InvalidState("Явное назначение заявки исходного набора не совпадает.")


async def validate_state(state: SavedState, settings: Settings) -> SavedState:
    validate_history(state)
    settings = effective_settings(settings, state.request)
    road = settings.routing_backend == "osrm"
    local_roads = settings.routing_backend == "local_roads"
    expected_model = ("osm_fixed_speed" if local_roads else
                      "osrm_road" if road else "haversine_estimate")
    if state.plan.map_data and state.plan.map_data.distance_model != expected_model:
        raise DistanceModelMismatch(state.plan.map_data.distance_model, expected_model)
    errors = await validate_plan(
        state.request, state.plan, PlanningService(settings).routing_provider()
    )
    if errors:
        raise InvalidState("Сохранённый план не соответствует запросу: " + ", ".join(errors[:8]))
    # Rebuild presentation data from the validated snapshot; never trust a stale stored diff/map.
    restored = state.model_copy(deep=True)
    if restored.plan.metrics.labor is None:
        restored.plan.metrics.labor = build_labor_metrics(
            restored.plan.routes, planning_time=restored.request.planning_time,
            remaining=restored.request.previous_plan is not None,
        )
    if restored.plan.metrics.workload is None:
        restored.plan.metrics.workload = build_workload_metrics(
            restored.plan.routes, planning_time=restored.request.planning_time,
        )
    restored.plan.diff = None
    restored.plan.map_data = build_map_data(
        restored.request, road_distances=road, local_roads=local_roads,
    )
    restored.plan.diagnostics["travel_model"] = build_travel_model(restored.request, settings)
    from app.services.districts import attach_district_summary

    attach_district_summary(restored.request, restored.plan)
    attach_previous_diff(restored.request, restored.plan)
    return restored


async def recalculate_state(state: SavedState, settings: Settings) -> SavedState:
    """Build a new plan from the saved current state without replaying its history.

    The old published plan may use an unavailable routing model. Only the new
    plan is checked against the current router; request/history still undergo
    their ordinary structural and consistency checks. A deep copy keeps the
    caller's snapshot intact, including execution intervals in previous_plan.
    """
    validate_history(state)
    recalculated = state.model_copy(deep=True)
    recalculated.plan = await PlanningService(settings).plan(
        recalculated.request.model_copy(deep=True),
    )
    recalculated.saved_at = datetime.now(UTC)
    return await validate_state(recalculated, settings)
