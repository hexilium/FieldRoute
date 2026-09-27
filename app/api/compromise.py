from fastapi import APIRouter, HTTPException

from app.api.progress import stream_planning
from app.api.routes import SettingsDependency
from app.domain.compromise import CompromiseRequest
from app.domain.models import PlanComparison
from app.services.compromise import InvalidCompromise, plan_compromise

router = APIRouter(prefix="/api/v1")


@router.post("/compromise", response_model=PlanComparison)
async def compromise(payload: CompromiseRequest, settings: SettingsDependency) -> PlanComparison:
    try:
        return await plan_compromise(payload, settings)
    except InvalidCompromise as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/compromise/stream")
async def compromise_stream(payload: CompromiseRequest, settings: SettingsDependency):
    return stream_planning(lambda: compromise(payload, settings))
