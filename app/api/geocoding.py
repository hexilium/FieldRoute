from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.services.geocoding import ATTRIBUTION, GeocodedPlace, GeocodingError, GeocodingService
from app.services.local_geocoding import LocalGeocodingService

router = APIRouter(prefix="/api/v1/geocoding")
Latitude = Annotated[float, Query(ge=-90, le=90, allow_inf_nan=False)]
Longitude = Annotated[float, Query(ge=-180, le=180, allow_inf_nan=False)]
Geocoder = GeocodingService | LocalGeocodingService


class SearchResult(BaseModel):
    results: list[GeocodedPlace]
    attribution: str = ATTRIBUTION


class ReverseResult(BaseModel):
    result: GeocodedPlace | None
    attribution: str = ATTRIBUTION


def get_geocoder(settings: Annotated[Settings, Depends(get_settings)]) -> Geocoder:
    if settings.geocoding_backend == "photon":
        return GeocodingService(settings)
    return LocalGeocodingService(settings)


@router.get("/search", response_model=SearchResult)
async def search(
    q: Annotated[str, Query(min_length=3, max_length=300)],
    geocoder: Annotated[Geocoder, Depends(get_geocoder)],
    lat: Annotated[float | None, Query(ge=-90, le=90, allow_inf_nan=False)] = None,
    lon: Annotated[float | None, Query(ge=-180, le=180, allow_inf_nan=False)] = None,
) -> dict:
    query = " ".join(q.split())
    if len(query) < 3:
        raise HTTPException(status_code=422, detail="Введите адрес: не менее трёх символов.")
    if (lat is None) != (lon is None):
        raise HTTPException(status_code=422, detail="Для области поиска нужны широта и долгота.")
    try:
        return {"results": await geocoder.search(query, lat, lon)}
    except GeocodingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.get("/reverse", response_model=ReverseResult)
async def reverse(
    lat: Latitude, lon: Longitude,
    geocoder: Annotated[Geocoder, Depends(get_geocoder)],
) -> dict:
    try:
        return {"result": await geocoder.reverse(lat, lon)}
    except GeocodingError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
