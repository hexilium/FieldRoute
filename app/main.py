import hashlib
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api.assignments import router as assignments_router
from app.api.compromise import router as compromise_router
from app.api.events import router as events_router
from app.api.explanations import router as explanations_router
from app.api.geocoding import router as geocoding_router
from app.api.imports import router as imports_router
from app.api.local_map import router as local_map_router
from app.api.progress import router as progress_router
from app.api.routes import router
from app.api.state import router as state_router
from app.api.settings import router as settings_router
from app.config import get_settings
from app.routing.base import UnsupportedTravelProfile
from app.routing.local_roads import RoutingCoverageError, RoutingUnavailable
from app.services.planner import UnsupportedOptimizationPolicy

settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="0.2.0",
    description="Planning and stable dynamic replanning of field engineer routes",
)
app.include_router(router)
app.include_router(assignments_router)
app.include_router(compromise_router)
app.include_router(imports_router)
app.include_router(events_router)
app.include_router(state_router)
app.include_router(settings_router)
app.include_router(explanations_router)
app.include_router(progress_router)
app.include_router(geocoding_router)
app.include_router(local_map_router)


@app.exception_handler(UnsupportedOptimizationPolicy)
@app.exception_handler(UnsupportedTravelProfile)
@app.exception_handler(RoutingCoverageError)
async def unsupported_policy(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(RoutingUnavailable)
async def routing_unavailable(request: Request, exc: RoutingUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


class RevalidatingStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if Path(path).suffix in {".js", ".css", ".html"}:
            response.headers["Cache-Control"] = "no-cache"
        return response


static_dir = Path(__file__).resolve().parent / "static"
asset_reference = re.compile(r'(["\'])/static/((?!vendor/)[^"\'?]+\.(?:js|css))(?:\?[^"\']*)?\1')
app.mount("/static", RevalidatingStaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
async def index() -> HTMLResponse:
    def versioned_asset(match: re.Match) -> str:
        quote, path = match.groups()
        digest = hashlib.sha256((static_dir / path).read_bytes()).hexdigest()[:12]
        return f"{quote}/static/{path}?v={digest}{quote}"

    # New URLs also invalidate assets cached before revalidation was enabled.
    html = asset_reference.sub(versioned_asset, (static_dir / "index.html").read_text())
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
