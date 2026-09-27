from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.domain.events import EventRequest, EventResult
from app.services.events import InvalidEvent, apply_event

router = APIRouter(prefix="/api/v1/events")


@router.post("/replan", response_model=EventResult)
async def replan_event(
    payload: EventRequest, settings: Annotated[Settings, Depends(get_settings)]
) -> EventResult:
    try:
        return await apply_event(payload, settings)
    except InvalidEvent as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
