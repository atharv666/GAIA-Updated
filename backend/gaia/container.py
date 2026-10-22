"""Service container: builds providers and services from settings (overridable in tests)."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .db.session import create_engine, init_db, make_session_factory
from .ingestion.service import IngestionService
from .pipeline_a.chat_service import ChatService
from .pipeline_a.farm_assistant import FarmAssistant
from .pipeline_a.open_assistant import OpenAssistant
from .pipeline_b.service import DiagnosisService
from .providers.embeddings import Embedder, build_embedder
from .providers.llm import LLM, build_llm
from .providers.websearch import WebSearch, build_search
from .rover.client import RoverClient

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    engine: sa.Engine
    session_factory: sessionmaker[Session]
    llm: LLM
    vlm: LLM
    embedder: Embedder
    search: WebSearch
    rover: RoverClient
    ingestion: IngestionService
    chat: ChatService
    diagnosis: DiagnosisService
    _scheduler: IngestionScheduler | None = field(default=None, repr=False)

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        engine: sa.Engine | None = None,
        llm: LLM | None = None,
        vlm: LLM | None = None,
        embedder: Embedder | None = None,
        search: WebSearch | None = None,
    ) -> Container:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        engine = engine or create_engine(settings)
        init_db(engine)
        session_factory = make_session_factory(engine)
        llm = llm or build_llm(settings, "chat")
        vlm = vlm or build_llm(settings, "vision")
        embedder = embedder or build_embedder(settings)
        search = search or build_search(settings)
        ingestion = IngestionService(session_factory, embedder, llm, settings)
        chat = ChatService(
            FarmAssistant(llm, embedder, ingestion, settings),
            OpenAssistant(llm, search, settings),
            settings,
        )
        diagnosis = DiagnosisService(session_factory, vlm, llm, search, embedder, settings)
        rover = RoverClient(settings.rover_base_url, settings.resolved_rover_stream_url, settings.rover_timeout_seconds)
        log.info(
            "GAIA ready: db=%s llm=%s vlm=%s embedder=%s search=%s",
            engine.dialect.name, llm.model_id, vlm.model_id, embedder.model_id, search.name,
        )
        return cls(settings, engine, session_factory, llm, vlm, embedder, search, rover, ingestion, chat, diagnosis)

    def start_background(self) -> None:
        recovered = self.diagnosis.recover_interrupted()
        if recovered:
            log.warning("Marked %d interrupted diagnosis job(s) as failed", recovered)
        if self.settings.scheduler_enabled and self._scheduler is None:
            self._scheduler = IngestionScheduler(self.ingestion, self.settings.ingest_interval_seconds)
            self._scheduler.start()

    def stop_background(self) -> None:
        if self._scheduler:
            self._scheduler.stop()
            self._scheduler = None

    def describe(self) -> dict:
        return {
            "database": self.engine.dialect.name,
            "llm": {"model": self.llm.model_id, "available": self.llm.available},
            "vlm": {"model": self.vlm.model_id, "available": self.vlm.available},
            "embedder": self.embedder.model_id,
            "search": {"provider": self.search.name, "available": self.search.available},
            "window_minutes": self.settings.window_minutes,
            "llm_digest_enabled": self.settings.llm_digest_enabled,
            "timezone": self.settings.farm_timezone,
        }


class IngestionScheduler:
    """Background thread that closes, aggregates and indexes sensor windows."""

    def __init__(self, ingestion: IngestionService, interval_s: int):
        self.ingestion = ingestion
        self.interval_s = max(5, interval_s)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="gaia-ingestion", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                # Open windows are re-aggregated (cheap, keeps charts live); closed ones are indexed.
                report = self.ingestion.sync(include_open=True)
                if report.windows_indexed:
                    log.info("Ingestion: %s", report.to_dict())
            except Exception:  # keep the loop alive
                log.exception("Background ingestion failed")
