"""Read-only defaults catalogue and side-effect-free portable settings validation."""
from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from app.config import Settings, get_settings
from app.domain.compromise_options import CompromiseOptions
from app.domain.models import (
    DistrictMode, EmergencyReplanPolicy, ObjectiveWeights, OptimizationPolicy, PlanRequest,
    ServicePriorityPolicy, UrgencyPolicy, UrgentStartPolicy,
)
from app.domain.planning_settings import ExecutionOptions
from app.services.planning_settings import execution_snapshot, factory_execution

router = APIRouter(prefix="/api/v1/settings")


class PlanningRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    district_mode: DistrictMode = PlanRequest.model_fields["district_mode"].default
    optimization_policy: OptimizationPolicy = PlanRequest.model_fields["optimization_policy"].default
    urgency_policy: UrgencyPolicy = PlanRequest.model_fields["urgency_policy"].default
    urgent_start_policy: UrgentStartPolicy = PlanRequest.model_fields["urgent_start_policy"].default
    service_priority_policy: ServicePriorityPolicy = PlanRequest.model_fields["service_priority_policy"].default
    emergency_replan_policy: EmergencyReplanPolicy = PlanRequest.model_fields["emergency_replan_policy"].default
    freeze_horizon_minutes: int = Field(default=PlanRequest.model_fields["freeze_horizon_minutes"].default, ge=0, le=525600, strict=True)


class SettingsValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: PlanningRules = Field(default_factory=PlanningRules)
    execution: ExecutionOptions = Field(default_factory=ExecutionOptions)
    weights: ObjectiveWeights = Field(default_factory=ObjectiveWeights)
    compromise: CompromiseOptions = Field(default_factory=CompromiseOptions)


class SettingsDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal["fieldroute-settings"] = "fieldroute-settings"
    format_version: Literal[1] = 1
    values: SettingsValues


def defaults(execution: dict) -> dict:
    return {
        "rules": PlanningRules().model_dump(mode="json"),
        "execution": execution,
        "weights": ObjectiveWeights().model_dump(mode="json"),
        "compromise": CompromiseOptions().model_dump(mode="json"),
    }


def field(group, section, name, title, kind, help, **kwargs):
    return dict(group=group, section=section, name=name, title=title, kind=kind, help=help, **kwargs)


FIELDS = [
    field("rules", "rules", "district_mode", "Работа по районам", "select",
          "В строгом режиме требуются district_id у каждого инженера и активной заявки. Ограничение касается назначений, не геометрии дороги.",
          choices=[["strict", "Каждый район отдельно, без выездов"], ["unrestricted", "Общий расчёт, выезды разрешены"]]),
    field("rules", "rules", "optimization_policy", "Главная цель", "select",
          "Цели сравниваются по очереди, а не складываются в деньги. Выбор синхронизируется с целью на странице плана.",
          choices=[["staff_first", "Меньше инженеров"], ["distance_first", "Меньше километров"], ["sla_first", "Соблюдать SLA"]]),
    field("rules", "rules", "service_priority_policy", "Приоритет типов работ", "select",
          "Правило организаторов: Авария → Подключение → Ремонт / Дозаказ. Это лексикографический приоритет назначения, а не вес в общей сумме.",
          choices=[["organizer", "Авария → Подключение → Ремонт / Дозаказ"], ["numeric", "Только числовой priority (старое поведение)"]]),
    field("rules", "rules", "emergency_replan_policy", "Авария в течение дня", "select",
          "По правилу организаторов новая авария может изменить ещё не начатую часть ранее сформированных маршрутов. Выполняемые и явно закреплённые работы не снимаются.",
          choices=[["reroute_future", "Перестраивать будущую часть маршрутов"], ["respect_freeze", "Сохранять горизонт закрепления"]]),
    field("rules", "rules", "urgency_policy", "Приоритет назначения срочных", "select",
          "При перепланировании: назначить срочные раньше общего покрытия или сначала назначить максимум работ. Явные закрепления сохраняются.",
          choices=[["urgent_first", "Сначала срочные"], ["coverage_first", "Сначала максимум назначений"]]),
    field("rules", "rules", "urgent_start_policy", "Как рано начинать срочные", "select",
          "Приоритет от 80 означает срочную работу. Последний режим допускает потерю обычных назначений ради срочных.",
          choices=[["after_primary", "После основной цели"], ["before_primary", "Перед основной целью"], ["before_coverage", "Раньше, даже ценой обычных заявок"]]),
    field("rules", "rules", "freeze_horizon_minutes", "Закрепить ближайшие визиты, мин", "integer",
          "Только при наличии предыдущего плана. 0 отключает автоматический горизонт, но не явные закрепления и не сохранение начатых работ.", min=0, max=525600),
    field("search", "execution", "search_strategy", "Стратегия поиска", "select",
          "full рассматривает прежний набор кандидатов; adaptive распределяет конечный бюджет попыток. Полнота перебора и глобальный оптимум не гарантируются.",
          choices=[["full", "Полный поиск (full)"], ["adaptive", "Адаптивный поиск (adaptive)"]]),
    field("search", "execution", "search_attempt_limit", "Бюджет проверок adaptive", "integer",
          "Общий лимит на запрос и одну цель, не на район. При 0 даже начальное распределение не строится. 60 000 не гарантируют покрытие на 500 заявках.", min=0, max=10000000, active="adaptive"),
    field("search", "execution", "search_slice_attempts", "Порция бюджета adaptive", "integer",
          "Сколько попыток выделять очередному оператору. Меняет траекторию поиска; увеличение не обещает лучший результат.", min=1, max=1000000, active="adaptive"),
    field("search", "execution", "search_time_limit_ms", "Лимит времени поиска, мс", "number",
          "Пусто: без лимита; 0: немедленная кооперативная остановка. Не ограничивает подготовку дорог, формирование ответа и валидацию. Для OSRM не поддерживается.", min=0, max=3600000, nullable=True),
    field("search", "execution", "search_workers", "Процессы полного поиска", "integer",
          "0: автоматический выбор; 1: последовательно; 2–3: процессы. Adaptive выполняется последовательно. Лимит времени также отключает процессный поиск.", min=0, max=3, active="full"),
    field("search", "execution", "search_parallel_min_jobs", "Порог для процессов, заявок", "integer",
          "При автоматическом выборе процессов: ниже порога полный поиск последовательный. При отдельных районах порог относится к размеру каждой подзадачи. Явные 2–3 процесса обходят этот порог.", min=0, max=100000, active="full"),
    field("routing", "execution", "routing_backend", "Модель переездов", "select",
          "Haversine оценивает расстояние без дорог. Дорожные сервисы должны быть заранее запущены на сервере. Ошибка сервиса не подменяется приблизительным расчётом.",
          choices=[["haversine", "Оценка по координатам (Haversine)"], ["local_roads", "Локальная дорожная сеть OSM"], ["osrm", "OSRM: автомобильный профиль"]]),
    field("routing", "execution", "haversine_road_factor", "Множитель расстояния Haversine", "number",
          "Умножает расстояние по прямой. Не применяется к OSRM или локальным дорогам и не моделирует пробки.", min=0.001, max=10, active="haversine"),
    *[field("routing", "execution", f"speed_{mode}_kmh", f"Скорость по умолчанию: {label}, км/ч", "number",
            "Используется для Haversine и local_roads, только если у инженера не задана собственная travel_speed_kmh. Для OSRM не используется.",
            min=0.001, max=200, active="fixed_speed")
      for mode, label in [("car", "автомобиль"), ("walk", "пешком"), ("bicycle", "велосипед"), ("public_transport", "общественный транспорт")]],
    field("routing", "execution", "roads_snap_radius_m", "Радиус привязки к дороге, м", "number",
          "Для local_roads. Слишком большой радиус может привязать координаты к далёкой дороге, он не исправляет адрес.", min=0.001, max=100000, active="local_roads"),
    field("routing", "execution", "roads_matrix_batch_size", "Размер пакета дорожной матрицы", "integer",
          "Для local_roads. Число точек в пакете запросов, от 1 до 50.", min=1, max=50, active="local_roads"),
    field("routing", "execution", "roads_matrix_concurrency", "Параллельные запросы дорог", "integer",
          "Для local_roads. Нагрузка на локальные дорожные сервисы; больше не обязательно быстрее.", min=1, max=16, active="local_roads"),
    *[field("weights", "weights", name, label, "number", help, min=0, max=1000000000)
      for name, label, help in [
        ("sla_violation", "За нарушение SLA", "За опоздавшую назначенную заявку; дополнительно учитываются минуты опоздания."),
        ("travel_minutes", "За минуту дороги", "Вес времени переездов в дополнительной стоимости."),
        ("distance_km", "За километр", "Взвешенный пробег при перепланировании; главная цель минимизации километров сравнивает расстояние напрямую."),
        ("plan_churn", "За смену инженера", "Действует при наличии предыдущего плана."),
        ("schedule_shift", "За минуту сдвига начала", "Действует при наличии предыдущего плана. Абсолютный сдвиг раньше или позже."),
        ("unassigned", "За неназначение", "Не влияет на основной insertion: покрытие сравнивается отдельным старшим критерием. Сохранено для экспериментальных алгоритмов."),
        ("overtime", "За переработку", "Не ослабляет смены insertion. Для основного алгоритма конец смены является жёстким ограничением."),
        ("workload_imbalance", "За неравномерную загрузку", "Не участвует в выборе основного insertion. Сохранено для экспериментальных алгоритмов."),
      ]],
    field("compromise", "compromise", "neighborhood", "Перестройки компромисса", "select",
          "Начальное значение отдельной формы SLA-компромисса. Extended не гарантирует превосходство basic на том же бюджете.",
          choices=[["basic", "Базовые"], ["extended", "Расширенные"]]),
    field("compromise", "compromise", "attempt_limit", "Проверок компромисса", "integer",
          "Отдельный лимит улучшения готового плана, не бюджет adaptive. Максимум 200 000.", min=0, max=200000),
    field("compromise", "compromise", "extra_distance_percent", "Дополнительный пробег, %", "number",
          "От исходного плана выбранной карточки. Повторный запуск не накапливает процент.", min=0, max=200),
    field("compromise", "compromise", "extra_engineers", "Дополнительные инженеры", "integer",
          "Сколько инженеров разрешить сверх числа в исходном плане. Не разрешает межрайонные назначения в строгом режиме.", min=0, max=10000),
    field("compromise", "compromise", "target_sla_percent", "Целевой SLA, %", "number",
          "Пусто: максимизировать SLA. С порогом: сначала достигнуть его, затем экономить ресурсы. Достижимость порога не гарантируется.", min=0, max=100, nullable=True),
]


@router.get("")
async def settings_catalog(settings: Annotated[Settings, Depends(get_settings)]) -> dict:
    return {
        "schema_version": 1,
        "factory_defaults": defaults(factory_execution()),
        "server_defaults": defaults(execution_snapshot(settings)),
        "fields": FIELDS,
        "scope": "request_local_not_server_configuration",
        "server": {"solver_backend": settings.solver_backend,
                   "geocoding_backend": settings.geocoding_backend},
        "server_only": [
            "Адреса OSRM/VROOM/геокодера и пути к дорожным данным и кешам",
            "Имя приложения, окружение, сетевые таймауты и настройки геокодера",
            "Внутренние ограничения операторов и защитные пределы API",
        ],
    }


@router.post("/validate", response_model=SettingsDocument)
async def validate_settings(payload: SettingsDocument) -> SettingsDocument:
    # Portable files use application defaults for omitted fields, never .env of
    # the machine on which they happen to be imported. No shared state is written.
    execution = ExecutionOptions.model_validate(factory_execution() | payload.values.execution.model_dump())
    if execution.routing_backend == "osrm" and execution.search_time_limit_ms is not None:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail="Для OSRM очистите лимит времени поиска.")
    return payload.model_copy(update={"values": payload.values.model_copy(update={"execution": execution})})
