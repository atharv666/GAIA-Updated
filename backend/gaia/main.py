"""FastAPI application: REST API under /api/v1 plus the web interface.

Run:  uvicorn gaia.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import chat, diagnoses, knowledge, live, rover, system, telemetry
from .config import Settings, get_settings
from .container import Container
from .web.routes import WEB_DIR
from .web.routes import router as web_router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = container or Container.build(settings or get_settings())
        app.state.container = c
        c.start_background()
        try:
            yield
        finally:
            c.stop_background()

    app = FastAPI(
        title="GAIA Farm Intelligence API",
        version=__version__,
        description=(
            "Pipeline A: sensor-grounded RAG chat (Query my farm / open-ended). "
            "Pipeline B: VLM crop diagnosis with a single web-search agent. "
            "Both share one knowledge store (pgvector)."
        ),
        lifespan=lifespan,
    )
    cfg = settings or (container.settings if container else get_settings())
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for module in (system, telemetry, chat, diagnoses, knowledge, rover, live):
        app.include_router(module.router, prefix="/api/v1")
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    app.include_router(web_router)
    return app


app = create_app()
