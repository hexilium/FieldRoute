from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.domain.assignments import AssignmentPreview, AssignmentRequest
from app.services.assignments import InvalidAssignment, preview_assignment

router = APIRouter(prefix="/api/v1/assignments")


@router.post("/preview", response_model=AssignmentPreview)
async def preview(
    payload: AssignmentRequest, settings: Annotated[Settings, Depends(get_settings)],
) -> AssignmentPreview:
    try:
        return await preview_assignment(payload, settings)
    except InvalidAssignment as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
