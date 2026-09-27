"""Resolve a request's configuration without changing process-wide Settings."""
from app.config import Settings
from app.domain.models import PlanRequest
from app.domain.planning_settings import ExecutionOptions

EXECUTION_FIELDS = tuple(ExecutionOptions.model_fields)


def execution_snapshot(settings: Settings) -> dict:
    return {name: getattr(settings, name) for name in EXECUTION_FIELDS}


def factory_execution() -> dict:
    # Use declared defaults, never Settings() which would read .env/os.environ.
    return {name: Settings.model_fields[name].default for name in EXECUTION_FIELDS}


def effective_settings(settings: Settings, request: PlanRequest) -> Settings:
    if request.execution is None:
        return settings
    options = ExecutionOptions.model_validate(request.execution.model_dump())
    return settings.model_copy(update=options.model_dump())


def assumed_speeds(settings: Settings) -> dict[str, float]:
    return {mode: getattr(settings, f"speed_{mode}_kmh")
            for mode in ("car", "walk", "bicycle", "public_transport")}
