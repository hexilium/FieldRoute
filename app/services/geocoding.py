"""Explicit address searches backed by a configurable Photon service."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import httpx
from pydantic import BaseModel, Field, ValidationError

from app.config import Settings

ATTRIBUTION = "© OpenStreetMap contributors"
CACHE_TTL_SECONDS = 86400
CACHE_MAX_ENTRIES = 2048


class GeocodingError(RuntimeError):
    def __init__(self, detail: str, status_code: int = 503):
        super().__init__(detail)
        self.status_code = status_code


class GeocodedPlace(BaseModel):
    lat: float = Field(ge=-90, le=90, allow_inf_nan=False)
    lon: float = Field(ge=-180, le=180, allow_inf_nan=False)
    label: str = Field(min_length=1)


class GeocodingCache:
    """Cache and one-request-per-second gate shared by workers on this machine."""

    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS geocoding_cache "
                               "(key TEXT PRIMARY KEY, expires REAL, value TEXT)")
            connection.execute("CREATE TABLE IF NOT EXISTS geocoding_rate "
                               "(host TEXT PRIMARY KEY, next_request REAL)")

    def connect(self):
        return closing(sqlite3.connect(self.path, timeout=1))

    def read(self, key: str) -> tuple[bool, object]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM geocoding_cache WHERE key=? AND expires>?",
                (key, time.time()),
            ).fetchone()
        return (True, json.loads(row[0])) if row else (False, None)

    def write(self, key: str, value: object) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO geocoding_cache VALUES (?, ?, ?)",
                (key, time.time() + CACHE_TTL_SECONDS, json.dumps(value, allow_nan=False)),
            )
            connection.execute(
                "DELETE FROM geocoding_cache WHERE key IN "
                "(SELECT key FROM geocoding_cache ORDER BY expires DESC LIMIT -1 OFFSET ?)",
                (CACHE_MAX_ENTRIES,),
            )
            connection.commit()

    def request_delay(self, host: str) -> float:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = time.time()
            row = connection.execute(
                "SELECT next_request FROM geocoding_rate WHERE host=?", (host,),
            ).fetchone()
            delay = max(0, row[0] - now) if row else 0
            if not delay:
                connection.execute(
                    "INSERT OR REPLACE INTO geocoding_rate VALUES (?, ?)", (host, now + 1.05),
                )
            connection.commit()
        return delay


class GeocodingService:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    async def search(self, query: str, lat: float | None, lon: float | None) -> list[dict]:
        params = {"q": query, "limit": "5"}
        if lat is not None and lon is not None:
            # Bias to the current map area; do not exclude a fully specified remote address.
            params.update(lat=f"{lat:.6f}", lon=f"{lon:.6f}")
        return await self.query("api", params)

    async def reverse(self, lat: float, lon: float) -> dict | None:
        results = await self.query(
            "reverse", {"lat": f"{lat:.6f}", "lon": f"{lon:.6f}", "limit": "1"},
        )
        return results[0] if results else None

    async def query(self, endpoint: str, params: dict[str, str]):
        url = self.settings.geocoding_url.strip().rstrip("/")
        if not url:
            raise GeocodingError(
                "Поиск адресов не настроен. Укажите GEOCODING_URL сервиса геокодирования. "
                "Координаты можно выбрать на карте или ввести вручную."
            )
        key = json.dumps([url, endpoint, params], sort_keys=True)
        try:
            cache = GeocodingCache(self.settings.geocoding_cache_path)
            hit, value = cache.read(key)
            if hit:
                return value
            timeout = self.settings.geocoding_timeout_seconds
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    timeout=timeout, transport=self.transport,
                    headers={"User-Agent": self.settings.geocoding_user_agent},
                ) as client:
                    while delay := cache.request_delay(httpx.URL(url).host):
                        await asyncio.sleep(delay)
                        hit, value = cache.read(key)
                        if hit:
                            return value
                    response = await client.get(f"{url}/{endpoint}", params=params)
            if response.status_code in {403, 429} or response.status_code >= 500:
                raise GeocodingError("Сервис поиска адресов временно недоступен. Попробуйте позже.")
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("features"), list):
                raise TypeError("Expected geocoding results")
            value = [self.place(item) for item in payload["features"][:int(params["limit"])]]
            cache.write(key, value)
            return value
        except (TimeoutError, httpx.RequestError) as exc:
            raise GeocodingError(
                "Сервис поиска адресов не отвечает. Попробуйте позже или выберите точку на карте."
            ) from exc
        except (ValueError, TypeError, KeyError, ValidationError, httpx.HTTPStatusError) as exc:
            raise GeocodingError(
                "Сервис поиска адресов вернул некорректный ответ. Попробуйте позже.", 502,
            ) from exc
        except (OSError, sqlite3.Error) as exc:
            raise GeocodingError("Поиск адресов временно недоступен: ошибка кеша сервиса.") from exc

    @staticmethod
    def place(payload: object) -> dict:
        if not isinstance(payload, dict):
            raise TypeError("Expected a geocoded place")
        geometry = payload["geometry"]
        properties = payload["properties"]
        if not isinstance(geometry, dict) or not isinstance(properties, dict):
            raise TypeError("Expected geometry and properties")
        coordinates = geometry["coordinates"]
        if geometry.get("type") != "Point" or not isinstance(coordinates, list):
            raise ValueError("Expected point coordinates")
        if len(coordinates) < 2:
            raise ValueError("Expected longitude and latitude")
        parts = []
        for field in ("name", "street", "housenumber", "district", "city", "county",
                      "state", "country"):
            part = properties.get(field)
            if part is None:
                continue
            if not isinstance(part, str):
                raise TypeError("Expected an address component")
            part = part.strip()
            if part and part.casefold() not in {entry.casefold() for entry in parts}:
                parts.append(part)
        return GeocodedPlace(
            lat=coordinates[1], lon=coordinates[0], label=", ".join(parts),
        ).model_dump()
