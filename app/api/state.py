import json
from math import isfinite
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.api.progress import stream_planning
from app.config import Settings, get_settings
from app.domain.state import SavedState
from app.services.state import (
    DistanceModelMismatch,
    InvalidState,
    recalculate_state,
    validate_history,
    validate_state,
)

router = APIRouter(prefix="/api/v1/states")
MAX_STATE_BYTES = 10 * 1024 * 1024
STATE_REQUEST_SCHEMA = {"requestBody": {"required": True, "content": {"application/json": {
    "schema": {"type": "object", "required": ["request", "plan"]},
    "example": {"request": {}, "plan": {}, "events": []},
}}}}


def finite_number(value: str) -> float:
    number = float(value)
    if not isfinite(number):
        raise ValueError("JSON contains a non-finite number")
    return number


async def read_saved_state(request: Request) -> SavedState:
    """Use the same bounded body parsing for restore and explicit recalculation."""
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_STATE_BYTES:
            raise HTTPException(status_code=413, detail="Состояние должно быть не больше 10 МиБ.")
        body.extend(chunk)
    try:
        raw = json.loads(
            body.decode("utf-8-sig"),
            parse_constant=finite_number, parse_float=finite_number,
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise HTTPException(status_code=422, detail="Не удалось прочитать JSON состояния.") from exc
    try:
        return SavedState.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        for error in errors:
            error["loc"] = ["body", *error["loc"]]
        raise HTTPException(status_code=422, detail=errors) from exc


@router.post("/validate", response_model=SavedState, openapi_extra=STATE_REQUEST_SCHEMA)
async def restore_state(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> SavedState | JSONResponse:
    """Validate and normalize a saved JSON snapshot, without rescheduling or server storage."""
    state = await read_saved_state(request)
    try:
        return await validate_state(state, settings)
    except DistanceModelMismatch as exc:
        return JSONResponse(status_code=422, content={
            "detail": str(exc), "code": "distance_model_mismatch",
            "saved_distance_model": exc.saved_distance_model,
            "current_distance_model": exc.current_distance_model,
        })
    except InvalidState as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def recalculate_saved_state(state: SavedState, settings: Settings) -> SavedState:
    try:
        return await recalculate_state(state, settings)
    except InvalidState as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/recalculate", response_model=SavedState, openapi_extra=STATE_REQUEST_SCHEMA)
async def recalculate(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
) -> SavedState:
    """Recalculate the saved request; the server never overwrites a stored snapshot."""
    state = await read_saved_state(request)
    return await recalculate_saved_state(state, settings)


@router.post("/recalculate/stream", openapi_extra=STATE_REQUEST_SCHEMA)
async def recalculate_stream(
    request: Request, settings: Annotated[Settings, Depends(get_settings)]
):
    state = await read_saved_state(request)
    # Reject a bad history before committing to an HTTP 200 streaming response.
    try:
        validate_history(state)
    except InvalidState as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return stream_planning(lambda: recalculate_saved_state(state, settings))
