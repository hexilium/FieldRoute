from datetime import timedelta

from app.config import Settings
from app.services.planning_settings import effective_settings
from app.domain.events import (
    CancelJobEvent,
    EngineerDelayEvent,
    EngineerUnavailableEvent,
    EventRequest,
    EventResult,
    NewJobEvent,
    UrgentJobEvent,
)
from app.domain.models import Job, JobStatus, PlanRequest
from app.services.planner import PlanningService
from app.services.stability import forced_assignment
from app.services.validation import validate_plan


class InvalidEvent(ValueError):
    pass


async def apply_event(payload: EventRequest, settings: Settings) -> EventResult:
    """Advance simulated execution using the published plan, then apply one event atomically."""
    settings = effective_settings(settings, payload.request)
    event, previous = payload.event, payload.plan
    if event.time < payload.request.planning_time:
        raise InvalidEvent("Время события не может быть раньше времени текущего плана.")
    # All event types use the solver that preserves executing visits and validates its output.
    service = PlanningService(settings.model_copy(update={"solver_backend": "insertion"}))
    errors = await validate_plan(payload.request, previous, service.routing_provider())
    if errors:
        raise InvalidEvent("План не соответствует исходному запросу: " + ", ".join(errors[:8]))

    state = payload.request.model_copy(deep=True)
    state.planning_time = event.time
    state.previous_plan = previous.model_copy(deep=True)
    jobs = {job.id: job for job in state.jobs}
    engineers = {engineer.id: engineer for engineer in state.engineers}
    completed, executing = [], []
    for route in previous.routes:
        engineer = engineers[route.engineer_id]
        for stop in route.stops:
            job = jobs[stop.job_id]
            if stop.arrival <= event.time:
                # Includes waiting at the customer's address. Travel in progress is not tracked.
                engineer.current_location = stop.location.model_copy(deep=True)
            if stop.departure <= event.time:
                job.status = JobStatus.completed
                completed.append(job.id)
            elif stop.service_start <= event.time:
                job.status = JobStatus.in_progress
                job.assigned_engineer_id = engineer.id
                engineer.available_from = max(
                    engineer.available_from or stop.departure, stop.departure
                )
                executing.append(job.id)

    released = []
    notices = [
        (
            "Ход работ смоделирован по показанному плану: завершённые визиты исключены, "
            "начатые сохраняют инженера и время окончания."
        ),
        "Позиция инженера — последний достигнутый адрес; движение между адресами не отслеживается.",
    ]
    if isinstance(event, (UrgentJobEvent, NewJobEvent)):
        if state.district_mode == "strict" and event.job.district_id is None:
            raise InvalidEvent("Для новой заявки в строгом режиме нужен district_id района.")
        if event.job.id in jobs:
            raise InvalidEvent("Заявка с таким ID уже существует, включая завершённые и отменённые.")
        if all(window.end < event.time for window in event.job.time_windows):
            kind = "срочной" if isinstance(event, UrgentJobEvent) else "внеплановой"
            raise InvalidEvent(f"Все окна {kind} заявки закончились до события.")
        new_job = Job.model_validate(event.job.model_dump())
        if isinstance(event, UrgentJobEvent):
            new_job.priority = 100
            new_job.metadata = dict(new_job.metadata) | {
                "dispatch_work_type": "emergency",
                "route_change_trigger": "emergency_event",
                "appeared_at": event.time.isoformat(),
            }
            notices.append(
                "Авария имеет максимальный диспетчерский приоритет. Для неё можно перестроить "
                "ещё не начатую часть опубликованных маршрутов; выполняемые и явно закреплённые "
                "работы сохраняются."
            )
        state.jobs.append(new_job)
    elif isinstance(event, CancelJobEvent):
        job = jobs.get(event.job_id)
        if job is None:
            raise InvalidEvent("Не найдена заявка для отмены.")
        if job.status in {JobStatus.in_progress, JobStatus.completed, JobStatus.cancelled}:
            raise InvalidEvent("Можно отменить только ещё не начатую и не отменённую заявку.")
        job.status = JobStatus.cancelled
    elif isinstance(event, EngineerUnavailableEvent):
        engineer = engineers.get(event.engineer_id)
        if engineer is None:
            raise InvalidEvent("Не найден инженер.")
        if not engineer.available:
            raise InvalidEvent("Инженер уже недоступен для новых работ.")
        freeze_until = event.time + timedelta(minutes=state.freeze_horizon_minutes)
        for route in previous.routes:
            if route.engineer_id != engineer.id:
                continue
            for stop in route.stops:
                job = jobs[stop.job_id]
                if (
                    job.status not in {JobStatus.completed, JobStatus.in_progress}
                    and event.time <= stop.service_start <= freeze_until
                    and forced_assignment(job, state)[2] == "freeze_horizon"
                ):
                    released.append(job.id)
        engineer.available = False
        notices.append(
            "Недоступный инженер завершает начатую работу и не получает будущие визиты. "
            "Автоматическое закрепление снято только с его будущих заявок; "
            "явные закрепления остаются и могут привести к неназначению."
        )
    elif isinstance(event, EngineerDelayEvent):
        engineer = engineers.get(event.engineer_id)
        if engineer is None:
            raise InvalidEvent("Не найден инженер.")
        if not engineer.available:
            raise InvalidEvent("Нельзя задержать инженера, недоступного для новых работ.")
        delayed_until = event.time + timedelta(minutes=event.delay_minutes)
        engineer.available_from = max(
            engineer.available_from or event.time,
            delayed_until,
        )
        notices.append(
            f"Инженер не получает новые визиты до {engineer.available_from.isoformat()}. "
            "Начатая работа сохраняет опубликованное время окончания."
        )

    state = PlanRequest.model_validate(state.model_dump())
    plan = await service.plan(state)
    return EventResult(
        request=state,
        plan=plan,
        event=event,
        completed_job_ids=completed,
        in_progress_job_ids=executing,
        released_freeze_job_ids=released,
        notices=notices,
    )
