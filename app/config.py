from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "FieldRoute"
    app_env: str = "dev"
    solver_backend: Literal["insertion", "baseline", "heuristic", "vroom"] = "insertion"
    routing_backend: Literal["haversine", "osrm", "local_roads"] = "haversine"
    vroom_url: str = "http://localhost:3000"
    osrm_url: str = "http://localhost:5000"
    roads_car_url: str = "http://127.0.0.1:5001"
    roads_foot_url: str = "http://127.0.0.1:5002"
    roads_bicycle_url: str = "http://127.0.0.1:5003"
    road_data_dir: str = ".local/roads"
    roads_snap_radius_m: float = Field(default=1000, gt=0, allow_inf_nan=False)
    roads_matrix_batch_size: int = Field(default=40, ge=1, le=50)
    roads_matrix_concurrency: int = Field(default=4, ge=1, le=16)
    search_time_limit_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    search_strategy: Literal["full", "adaptive"] = "full"
    search_attempt_limit: int = Field(default=60_000, ge=0)
    search_slice_attempts: int = Field(default=2_000, ge=1)
    search_workers: int = Field(default=0, ge=0, le=3)
    search_parallel_min_jobs: int = Field(default=200, ge=0)
    haversine_road_factor: float = Field(default=1.25, gt=0, le=10, allow_inf_nan=False)
    speed_car_kmh: float = Field(default=28.0, gt=0, le=200, allow_inf_nan=False)
    speed_walk_kmh: float = Field(default=5.0, gt=0, le=200, allow_inf_nan=False)
    speed_bicycle_kmh: float = Field(default=15.0, gt=0, le=200, allow_inf_nan=False)
    speed_public_transport_kmh: float = Field(default=18.0, gt=0, le=200, allow_inf_nan=False)
    geocoding_backend: Literal["local", "photon"] = "local"
    geodata_path: str | None = None
    local_geocoding_radius_m: float = Field(default=150, gt=0, allow_inf_nan=False)
    geocoding_url: str = "https://photon.komoot.io"
    geocoding_user_agent: str = Field(default="FieldRoute/0.2 (address selection)", min_length=1)
    geocoding_timeout_seconds: float = Field(default=10, gt=0, allow_inf_nan=False)
    geocoding_cache_path: str = ".local/geocoding.sqlite3"

    @field_validator("search_attempt_limit", "search_slice_attempts", mode="before")
    @classmethod
    def validate_attempt_integer(cls, value):
        if isinstance(value, (bool, float)):
            raise ValueError("attempt limits must be integers")
        return value

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
