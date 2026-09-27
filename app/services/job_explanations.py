"""Counterfactual checks of one job; never changes or reoptimizes the published plan."""

from time import perf_counter
from typing import Literal

from app.config import Settings
from app.services.planning_settings import effective_settings
from app.domain.explanations import (
    AssignmentAlternative,
    ConstraintEvidence,
    ExplanationRequest,
    JobExplanation,
    ObjectiveDifference,
)
from app.domain.models import Engineer, Job, JobStatus
from app.services.explainer import (
    SKILL_NAMES,
    TRANSPORT_NAMES,
    assignment_explanation,
    at,
    minutes_text,
    names,
)
from app.services.planner import PlanningService
from app.services.stability import forced_assignment
from app.services.validation import validate_plan
from app.solvers.insertion import Search
from app.solvers.scheduling import ScheduleFailure

CRITERIA = {
    "locked_unassigned": "Неназначенные закреплённые заявки",
    "urgent_unassigned_on_replan": "Неназначенные срочные при пересчёте",
    "urgent_unassigned": "Неназначенные срочные заявки",
    "urgent_start_minutes": "Суммарное время до начала срочных работ, мин",
    "unassigned": "Все неназначенные заявки",
    "used_engineers": "Задействованные инженеры",
    "distance_km": "Общий пробег, км",
    "sla_missed_jobs": "Заявки с несоблюдённым SLA",
    "late_minutes": "Суммарное опоздание, мин",
    "soft_cost": "Стоимость времени и изменений, условные единицы",
    "weighted_residual_cost": "Стоимость остатка смены, условные единицы",
}
SCOPE = (
    "Проверяется перенос только этой заявки. Исполнители и относительный порядок "
    "остальных работ сохраняются; времена в затронутых маршрутах пересчитываются. "
    "Это локальная проверка показанного плана, а не доказательство общего оптимума."
)


class InvalidExplanation(ValueError):
    pass


def eligibility_evidence(
    search: Search, job: Job, engineer: Engineer, code: str
) -> ConstraintEvidence:
    facts = {}
    if code == "district_mismatch":
        facts = {"job_district_id": job.district_id, "engineer_district_id": engineer.district_id,
                 "district_mode": search.request.district_mode.value}
        message = f"Заявка в районе {job.district_id}; инженер закреплён за районом {engineer.district_id}. Выезды запрещены."
    elif code == "skills":
        missing = job.required_skills - engineer.skills
        facts = {"missing_skills": sorted(missing)}
        message = "Не хватает навыков: " + names(missing, SKILL_NAMES)
    elif code == "equipment":
        missing = job.required_equipment - engineer.equipment
        facts = {"missing_equipment": sorted(missing)}
        message = "Нет оборудования: " + names(missing)
    elif code == "transport":
        facts = {
            "required_transport": job.required_transport,
            "available_transport": sorted(engineer.transport_modes),
        }
        message = "Нет требуемого транспорта: " + TRANSPORT_NAMES.get(
            job.required_transport, job.required_transport
        )
    elif code == "locked_to_other_engineer":
        owner, _, reason = forced_assignment(job, search.request)
        facts = {"owner_id": owner, "lock_reason": reason}
        owner_name = search.evaluator.engineers[owner].name
        message = f"Исполнитель закреплён: {owner_name} ({owner})"
        if reason == "freeze_horizon":
            message += "; начало предыдущего визита попадает в период сохранения назначений"
        else:
            message += "; явное закрепление или статус назначенной работы"
    else:
        facts = {"available": engineer.available}
        message = "Инженер отмечен недоступным"
    return ConstraintEvidence(
        code=code, job_id=job.id, stage="eligibility", message=message, facts=facts, positions=0
    )


def failure_evidence(
    search: Search,
    engineer_id: str,
    failure: ScheduleFailure,
    stage: Literal["source_route", "target_route"] = "target_route",
) -> ConstraintEvidence:
    facts = dict(failure.facts or {})
    job = search.evaluator.jobs.get(failure.job_id)
    prefix = f"Заявка {job.id} «{job.title}»: " if job else "Маршрут: "
    if failure.code == "time_window":
        action = "окончание" if facts["window_semantics"] == "completion" else "начало"
        message = (
            f"{action} {at(facts['constraint_time'])} позже конца окна "
            f"{at(facts['window_end'])} на {minutes_text(facts['overrun_minutes'])} мин"
        )
    elif failure.code == "shift_end":
        message = (
            f"окончание {at(facts['departure'])} позже конца смены "
            f"{at(facts['shift_end'])} на {minutes_text(facts['overrun_minutes'])} мин"
        )
    elif failure.code == "max_jobs":
        message = f"получается {facts['jobs']} работ при лимите {facts['limit']}"
    elif failure.code == "unreachable":
        message = "маршрутизатор не нашёл доступного пути"
    else:
        evidence = eligibility_evidence(
            search, job, search.evaluator.engineers[engineer_id], failure.code
        )
        facts.update(evidence.facts)
        message = evidence.message
    return ConstraintEvidence(
        code=failure.code,
        job_id=failure.job_id,
        stage=stage,
        message=prefix + message,
        facts=facts,
        positions=1 if stage == "target_route" else 0,
    )


async def explain_job(payload: ExplanationRequest, settings: Settings) -> JobExplanation:
    started = perf_counter()
    settings = effective_settings(settings, payload.request)
    request, plan = payload.request, payload.plan
    routing = PlanningService(settings).routing_provider()
    await routing.prepare(request)
    errors = await validate_plan(request, plan, routing)
    if errors:
        raise InvalidExplanation("План не соответствует запросу: " + ", ".join(errors[:8]))
    search = Search(request, routing)
    evaluator = search.evaluator
    job = evaluator.jobs.get(payload.job_id)
    if job is None:
        raise InvalidExplanation("Для разбора нужна активная заявка из показанного набора.")

    # Keep the published times, including additional waiting that validation permits.
    # Reconstruct costs through the same aggregation as search, not a second objective.
    base = await evaluator.published_routes(plan)
    chosen = next((eid for eid, route in base.items() if job.id in route.assigned_job_ids), None)
    current_key = search.key(base)
    current_km = sum(r.distance for r in base.values())
    current_travel = sum(r.travel for r in base.values())
    current_used = sum(bool(r.visits) for r in base.values())

    summary = []
    if chosen:
        selected = next(v for v in base[chosen].visits if v.job.id == job.id)
        summary.append(f"В показанном плане: {evaluator.engineers[chosen].name} ({chosen}).")
        if job.status == JobStatus.in_progress:
            summary.append(
                f"Выполняется с {at(selected.start)} до {at(selected.end)}; "
                f"осталось {minutes_text((selected.end - request.planning_time).total_seconds() / 60)} мин."
            )
        else:
            summary.extend(
                assignment_explanation(
                    job,
                    evaluator.engineers[chosen],
                    selected.minutes,
                    arrival=selected.arrival,
                    start=selected.start,
                    end=selected.end,
                )
            )
        owner, _, reason = forced_assignment(job, request)
        if owner:
            summary.append(
                "Исполнитель закреплён; будущие времена могут изменяться. Основание: "
                + (
                    "период сохранения назначений"
                    if reason == "freeze_horizon"
                    else "явное назначение"
                )
            )
    else:
        summary.append("Заявка не включена в показанный план.")
        summary.append(
            f"Работа {job.service_minutes} мин; навыки: {names(job.required_skills, SKILL_NAMES) or 'не требуются'}."
        )
        for window in job.time_windows:
            summary.append(
                f"Окно {at(window.start)} — {at(window.end)}; "
                + (
                    "работа целиком внутри окна"
                    if job.window_semantics == "completion"
                    else "начать внутри окна"
                )
            )

    alternatives = []
    for eid, engineer in evaluator.engineers.items():
        common = {
            "engineer_id": eid,
            "engineer_name": engineer.name,
            "is_current_engineer": eid == chosen,
        }
        if job.status == JobStatus.in_progress:
            alternatives.append(
                AssignmentAlternative(
                    **common,
                    feasible=eid == chosen,
                    comparison="executing" if eid == chosen else "infeasible",
                    blockers=[]
                    if eid == chosen
                    else [
                        ConstraintEvidence(
                            code="in_progress",
                            job_id=job.id,
                            stage="eligibility",
                            positions=0,
                            message="Работа уже выполняется; передача другому инженеру запрещена.",
                        )
                    ],
                )
            )
            continue
        reasons = evaluator.eligibility(job, engineer)
        if reasons:
            alternatives.append(
                AssignmentAlternative(
                    **common,
                    feasible=False,
                    comparison="infeasible",
                    blockers=[
                        eligibility_evidence(search, job, engineer, code) for code in reasons
                    ],
                )
            )
            continue
        trial = dict(base)
        if chosen and chosen != eid:
            reduced, failure = await evaluator.diagnose(
                chosen, tuple(j for j in base[chosen].jobs if j != job.id)
            )
            if failure:
                alternatives.append(
                    AssignmentAlternative(
                        **common,
                        feasible=False,
                        comparison="infeasible",
                        blockers=[failure_evidence(search, chosen, failure, "source_route")],
                    )
                )
                continue
            trial[chosen] = reduced
        sequence = tuple(j for j in base[eid].jobs if j != job.id)
        best = base if eid == chosen else None
        best_key = current_key if best else None
        failures = {}
        for position in range(len(sequence) + 1):
            schedule, failure = await evaluator.diagnose(
                eid, sequence[:position] + (job.id,) + sequence[position:]
            )
            if failure:
                key = failure.code, failure.job_id
                evidence = failure_evidence(search, eid, failure)
                evidence.example_position = position + 1 + bool(evaluator.executing.get(eid))
                if key not in failures:
                    failures[key] = evidence
                else:
                    previous = failures[key]
                    count = previous.positions + 1
                    if evidence.facts.get("overrun_minutes", float("inf")) < previous.facts.get(
                        "overrun_minutes", float("inf")
                    ):
                        failures[key] = evidence
                    failures[key].positions = count
                continue
            candidate = {**trial, eid: schedule}
            key = search.key(candidate)
            if best is None or key < best_key:
                best, best_key = candidate, key
        if best is None:
            alternatives.append(
                AssignmentAlternative(
                    **common,
                    feasible=False,
                    comparison="infeasible",
                    positions_tested=len(sequence) + 1,
                    blockers=list(failures.values()),
                )
            )
            continue
        position, visit = next(
            (i, v) for i, v in enumerate(best[eid].visits, 1) if v.job.id == job.id
        )
        difference = next(
            (
                ObjectiveDifference(
                    criterion=criterion,
                    current=old,
                    alternative=new,
                    message=(
                        f"{CRITERIA[criterion]}: {old:g} → {new:g}"
                        if f"{old:g}" != f"{new:g}"
                        else f"{CRITERIA[criterion]}: изменение {new - old:+.3g}"
                    ),
                )
                for criterion, old, new in zip(search.objective_order(), current_key, best_key)
                if old != new
            ),
            None,
        )
        alternatives.append(
            AssignmentAlternative(
                **common,
                feasible=True,
                positions_tested=len(sequence) + 1,
                position=position,
                arrival=visit.arrival,
                service_start=visit.start,
                departure=visit.end,
                leg_distance_km=visit.km,
                leg_travel_minutes=visit.minutes,
                plan_distance_delta_km=sum(r.distance for r in best.values()) - current_km,
                plan_travel_delta_minutes=sum(r.travel for r in best.values()) - current_travel,
                used_engineers_delta=sum(bool(r.visits) for r in best.values()) - current_used,
                objective_value=list(best_key),
                first_difference=difference,
                comparison="better"
                if best_key < current_key
                else "worse"
                if best_key > current_key
                else "equal",
            )
        )

    if job.status == JobStatus.in_progress:
        summary.append(
            "Работа уже выполняется. Её интервал сохраняется; перенос не рассматривается."
        )
    elif any(a.comparison == "better" for a in alternatives):
        summary.append(
            "Найдена локальная альтернатива с лучшей оценкой. Показанный план сохранён; ограниченный поиск не гарантирует оптимум."
        )
    elif not chosen:
        summary.append(
            "Есть допустимые вставки, но они не улучшают выбранную цель."
            if any(a.feasible for a in alternatives)
            else "Допустимой вставки в текущие маршруты не найдено."
            if alternatives
            else "В наборе нет инженеров."
        )
    elif any(
        a.feasible and not a.is_current_engineer and a.comparison == "equal" for a in alternatives
    ):
        summary.append("Есть равнозначный перенос к другому инженеру по всем критериям цели.")
    else:
        summary.append("Среди проверенных переносов улучшения выбранной цели не найдено.")
    if plan.solver == "fifo-baseline-v1":
        summary.append(
            "FIFO назначал в порядке входных данных. Альтернативы ниже проверены на конечном плане, после обработки всех заявок."
        )
    return JobExplanation(
        job_id=job.id,
        title=job.title,
        chosen_engineer_id=chosen,
        optimization_policy=request.optimization_policy,
        urgency_policy=request.urgency_policy,
        urgent_start_policy=request.urgent_start_policy,
        summary=summary,
        scope=SCOPE,
        objective_order=search.objective_order(),
        current_objective=list(current_key),
        alternatives=alternatives,
        analysis_time_ms=round((perf_counter() - started) * 1000, 2),
    )
