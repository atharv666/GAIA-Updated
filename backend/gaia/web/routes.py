"""Server-rendered web interface (same features as the mobile app, in a browser)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from .. import __version__

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=WEB_DIR / "templates")

router = APIRouter(include_in_schema=False)

PAGES = [
    ("overview", "/", "Overview", "dashboard"),
    ("rover", "/rover", "Rover", "rover"),
    ("diagnose", "/diagnose", "Diagnose", "scan"),
    ("chat", "/chat", "Gaia AI", "spark"),
    ("sensors", "/sensors", "Sensors", "gauge"),
    ("knowledge", "/knowledge", "Knowledge", "book"),
]


def _render(request: Request, page: str) -> HTMLResponse:
    c = request.app.state.container
    return templates.TemplateResponse(
        request,
        f"{page}.html",
        {
            "page": page,
            "pages": PAGES,
            "version": __version__,
            "farm_name": c.settings.farm_name,
            "farmer_name": c.settings.farmer_name,
            "timezone": c.settings.farm_timezone,
            "api_key_required": bool(c.settings.api_key),
            "rover_stream_url": c.rover.stream_url,
        },
    )


@router.get("/", response_class=HTMLResponse)
def overview(request: Request):
    return _render(request, "overview")


@router.get("/rover", response_class=HTMLResponse)
def rover(request: Request):
    return _render(request, "rover")


@router.get("/diagnose", response_class=HTMLResponse)
def diagnose(request: Request):
    return _render(request, "diagnose")


@router.get("/chat", response_class=HTMLResponse)
def chat(request: Request):
    return _render(request, "chat")


@router.get("/sensors", response_class=HTMLResponse)
def sensors(request: Request):
    return _render(request, "sensors")


@router.get("/knowledge", response_class=HTMLResponse)
def knowledge(request: Request):
    return _render(request, "knowledge")
