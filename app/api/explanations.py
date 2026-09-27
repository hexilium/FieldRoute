from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from app.config import Settings, get_settings
from app.domain.explanations import ExplanationRequest, JobExplanation
from app.services.job_explanations import InvalidExplanation, explain_job

router = APIRouter(prefix="/api/v1/explanations")


@router.post("/job", response_model=JobExplanation)
async def job_explanation(
    payload: ExplanationRequest, settings: Annotated[Settings, Depends(get_settings)]
) -> JobExplanation:
    try:
        return await explain_job(payload, settings)
    except InvalidExplanation as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
