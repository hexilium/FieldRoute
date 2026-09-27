"""Atomic local reassignment with a reviewable, independently validated preview."""

from time import perf_counter

from app.config import Settings
from app.services.planning_settings import effective_settings
from app.domain.assignments import AssignmentPreview, AssignmentRequest
from app.domain.events import ManualAssignmentEvent
from app.domain.explanations import ConstraintEvidence
from app.domain.models import JobStatus, PlanRequest
from app.services.job_explanations import eligibility_evidence, failure_evidence
from app.services.map_data import build_map_data
from app.services.districts import attach_district_summary
from app.services.planner import PlanningService, build_travel_model
from app.solvers.insertion import Search

SCOPE = (
    "Меняется только выбранный исполнитель. Назначения и относительный порядок остальных "
    "работ сохраняются; времена двух затронутых маршрутов пересчитываются. "
    "Часы сценария не продвигаются. После применения выбранный инженер будет закреплён "
    "за заявкой при следующих пересчётах."
)


class InvalidAssignment(ValueError):
    pass


def rejected(blockers: list[ConstraintEvidence]) -> AssignmentPreview:
    return AssignmentPreview(accepted=False, blockers=blockers, scope=SCOPE)


async def preview_assignment(payload: AssignmentRequest, settings: Settings) -> AssignmentPreview:
    from app.services.validation import validate_plan

    started = perf_counter()
    settings = effective_settings(settings, payload.request)
    original, published = payload.request, payload.plan
    jobs = {job.id: job for job in original.jobs}
    if payload.job_id not in jobs:
        raise InvalidAssignment("Не найдена заявка для ручного назначения.")
    if payload.engineer_id not in {engineer.id for engineer in original.engineers}:
        raise InvalidAssignment("Не найден выбранный инженер.")
    routing = PlanningService(settings).routing_provider()
    await routing.prepare(original)
    errors = await validate_plan(original, published, routing)
    if errors:
        raise InvalidAssignment("План не соответствует исходному запросу: " + ", ".join(errors[:8]))

    job = jobs[payload.job_id]
    if job.status in {JobStatus.in_progress, JobStatus.completed, JobStatus.cancelled}:
        return rejected([ConstraintEvidence(
            code=job.status.value, job_id=job.id, stage="eligibility", positions=0,
            message=("Начатую работу нельзя передать другому инженеру."
                     if job.status == JobStatus.in_progress else
                     "Завершённую или отменённую заявку нельзя назначить заново."),
        )])

    engineer = next(e for e in original.engineers if e.id == payload.engineer_id)
    if original.district_mode == "strict" and engineer.district_id != job.district_id:
        return rejected([ConstraintEvidence(
            code="district_mismatch", job_id=job.id, stage="eligibility", positions=0,
            message=f"Инженер относится к району {engineer.district_id}, заявка к {job.district_id}. Выезды запрещены.",
            facts={"engineer_district_id": engineer.district_id, "job_district_id": job.district_id},
        )])

    state = original.model_copy(deep=True)
    state.previous_plan = published.model_copy(deep=True)
    target = next(item for item in state.jobs if item.id == payload.job_id)
    # This explicit decision overrides only the target's old owner/freeze. It
    # does not infer execution from scheduled times or change any job status.
    target.assigned_engineer_id = payload.engineer_id
    target.locked = True
    state = PlanRequest.model_validate(state.model_dump())
    search = Search(state, routing)
    evaluator = search.evaluator
    target = search.by_id[payload.job_id]
    engineer = evaluator.engineers[payload.engineer_id]
    reasons = evaluator.eligibility(target, engineer)
    if reasons:
        return rejected([eligibility_evidence(search, target, engineer, code) for code in reasons])

    routes = await evaluator.published_routes(published)
    source_id = next((eid for eid, route in routes.items() if target.id in route.jobs), None)
    before_key = search.key(routes)
    best = None
    attempts = 0
    if source_id == engineer.id:
        # Locking the displayed owner should not unexpectedly reorder its route.
        best = routes
    else:
        if source_id is not None:
            reduced, failure = await evaluator.diagnose(
                source_id, tuple(jid for jid in routes[source_id].jobs if jid != target.id),
            )
            if failure:
                return rejected([failure_evidence(search, source_id, failure, "source_route")])
            assert reduced is not None
            routes[source_id] = reduced
        sequence = routes[engineer.id].jobs
        failures: dict[tuple[str, str | None], ConstraintEvidence] = {}
        best_key = None
        for position in range(len(sequence) + 1):
            attempts += 1
            route, failure = await evaluator.diagnose(
                engineer.id, sequence[:position] + (target.id,) + sequence[position:],
            )
            if failure:
                key = failure.code, failure.job_id
                if key not in failures:
                    evidence = failure_evidence(search, engineer.id, failure)
                    evidence.example_position = position + 1 + bool(evaluator.executing.get(engineer.id))
                    failures[key] = evidence
                else:
                    failures[key].positions += 1
                continue
            assert route is not None
            candidate = {**routes, engineer.id: route}
            key = search.key(candidate)
            if best is None or key < best_key:
                best, best_key = candidate, key
        if best is None:
            return rejected(list(failures.values()))

    expected = {stop.job_id for route in published.routes for stop in route.stops} | {target.id}
    actual = {visit.job.id for route in best.values() for visit in route.visits}
    if actual != expected:
        raise RuntimeError("Manual assignment must preserve every existing assignment")
    plan = await search.result(
        best, "manual-assignment-v1", started, None, manual_assignment=target.id,
    )
    plan.diagnostics.update({
        "manual_assignment": {
            "job_id": target.id, "engineer_id": engineer.id,
            "previous_engineer_id": source_id, "positions_tested": attempts,
            "source_objective_value": list(before_key),
        },
        "travel_model": build_travel_model(state, settings),
    })
    attach_district_summary(state, plan)
    plan.map_data = build_map_data(
        state, local_roads=settings.routing_backend == "local_roads",
        road_distances=settings.routing_backend == "osrm",
    )
    notices = [
        "Вариант проверен. Показанный план изменится только после нажатия «Применить».",
        "Исполнитель выбран вручную; вариант может уступать автоматическому плану по цели.",
    ]
    if source_id == engineer.id:
        notices.append("Сохранены показанные времена; текущий исполнитель станет явно закреплённым.")
    if job.locked or job.assigned_engineer_id:
        notices.append("Прежнее явное назначение выбранной заявки заменяется ручным решением.")
    return AssignmentPreview(
        accepted=True, request=state, plan=plan,
        event=ManualAssignmentEvent(
            type="manual_assignment", time=state.planning_time,
            job_id=target.id, engineer_id=engineer.id,
        ), notices=notices, scope=SCOPE,
    )
