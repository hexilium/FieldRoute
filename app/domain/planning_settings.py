"""Portable calculation overrides. Never contain service URLs, paths or secrets."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class ExecutionOptions(BaseModel):
    """Only explicitly supplied fields override server settings, including explicit null time."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    search_strategy: Literal["full", "adaptive"] | None = None
    search_attempt_limit: int | None = Field(default=None, ge=0, le=10_000_000)
    search_slice_attempts: int | None = Field(default=None, ge=1, le=1_000_000)
    search_time_limit_ms: float | None = Field(default=None, ge=0, le=3_600_000)
    search_workers: int | None = Field(default=None, ge=0, le=3)
    search_parallel_min_jobs: int | None = Field(default=None, ge=0, le=100_000)
    routing_backend: Literal["haversine", "local_roads", "osrm"] | None = None
    roads_snap_radius_m: float | None = Field(default=None, gt=0, le=100_000)
    roads_matrix_batch_size: int | None = Field(default=None, ge=1, le=50)
    roads_matrix_concurrency: int | None = Field(default=None, ge=1, le=16)
    haversine_road_factor: float | None = Field(default=None, gt=0, le=10)
    speed_car_kmh: float | None = Field(default=None, gt=0, le=200)
    speed_walk_kmh: float | None = Field(default=None, gt=0, le=200)
    speed_bicycle_kmh: float | None = Field(default=None, gt=0, le=200)
    speed_public_transport_kmh: float | None = Field(default=None, gt=0, le=200)

    @model_validator(mode="after")
    def null_only_for_time_limit(self):
        for name in self.model_fields_set - {"search_time_limit_ms"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name}: null не задаёт значение; удалите поле для наследования сервера")
        return self

    @model_serializer(mode="wrap")
    def omit_inherited_fields(self, handler):
        # Preserve omission through PlanRequest dumps, comparisons, district copies,
        # events and saved snapshots. Null is meaningful only for the deadline.
        return {key: value for key, value in handler(self).items() if key in self.model_fields_set}
