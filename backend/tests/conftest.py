from __future__ import annotations

import os
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from io import BytesIO
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

os.environ.setdefault("SCHEDULER_ENABLED", "false")

from gaia.config import Settings  # noqa: E402
from gaia.container import Container  # noqa: E402
from gaia.ingestion.telemetry import TelemetryBatch, ingest_batch  # noqa: E402
from gaia.providers.embeddings import HashingEmbedder  # noqa: E402
from gaia.providers.llm import LLM, ChatTurn, LLMError  # noqa: E402
from gaia.providers.websearch import WebResult, WebSearch  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
# A fixed "now" in the past keeps date-dependent assertions deterministic.
FIXED_NOW = datetime(2025, 6, 15, 15, 0, tzinfo=IST).astimezone(timezone.utc)


class ScriptedLLM(LLM):
    """Test double: ``handlers`` maps schema name (or 'text') to a callable returning the output."""

    provider = "scripted"

    def __init__(self, handlers: dict[str, Callable] | None = None):
        super().__init__("test-model")
        self.handlers = handlers or {}
        self.calls: list[tuple[str, str, list[ChatTurn]]] = []

    def _generate(self, *, system, turns, images, temperature, max_tokens, json_mode) -> str:
        self.calls.append(("text", system, list(turns)))
        handler = self.handlers.get("text")
        if handler is None:
            raise LLMError("no text handler")
        return handler(system, turns)

    def structured(self, *, system, turns, schema, images=(), temperature=0.1, max_tokens=4096):
        self.calls.append((schema.__name__, system, list(turns)))
        handler = self.handlers.get(schema.__name__)
        if handler is None:
            raise LLMError(f"no handler for {schema.__name__}")
        out = handler(system, turns)
        return out if isinstance(out, schema) else schema.model_validate(out)

    def schemas_called(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakeSearch(WebSearch):
    name = "fake"

    def __init__(self, results: list[WebResult] | None = None):
        self.results = results or [
            WebResult("Early blight of tomato", "https://extension.umn.edu/diseases/early-blight-tomato",
                      "Early blight causes brown lesions with concentric rings on older leaves. Remove infected "
                      "leaves, mulch, and rotate crops."),
            WebResult("Managing early blight", "https://example-farm-blog.com/early-blight",
                      "Copper or chlorothalonil fungicides can help; follow label directions."),
        ]
        self.queries: list[str] = []

    def search(self, query: str, max_results: int) -> list[WebResult]:
        self.queries.append(query)
        return [WebResult(r.title, r.url, r.snippet, query) for r in self.results[:max_results]]


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{(tmp_path / 'gaia.db').as_posix()}",
        data_dir=tmp_path,
        scheduler_enabled=False,
        farm_timezone="Asia/Kolkata",
        window_minutes=15,
        ingest_grace_seconds=60,
        device_key="test-device-key",
        api_key=None,
        gemini_api_key=None,
        openai_compat_api_key=None,
        embedding_provider="hashing",
        search_provider="none",
    )


@pytest.fixture
def llm() -> ScriptedLLM:
    return ScriptedLLM()


@pytest.fixture
def vlm() -> ScriptedLLM:
    return ScriptedLLM()


@pytest.fixture
def search() -> FakeSearch:
    return FakeSearch()


@pytest.fixture
def container(settings, llm, vlm, search) -> Container:
    return Container.build(settings, llm=llm, vlm=vlm, embedder=HashingEmbedder(settings.embedding_dim), search=search)


@pytest.fixture
def client(container):
    from fastapi.testclient import TestClient

    from gaia.main import create_app

    with TestClient(create_app(container=container)) as c:
        yield c


def seed_series(
    container: Container,
    *,
    start_local: datetime,
    minutes: int,
    values: Callable[[int], dict],
    step_s: int = 60,
    device_id: str = "rover-01",
    state: dict | None = None,
) -> None:
    """Insert one reading every ``step_s`` seconds; ``values(i)`` returns the sensor dict for sample i."""
    readings = []
    for i in range(minutes * 60 // step_s):
        ts = (start_local + timedelta(seconds=i * step_s)).astimezone(timezone.utc)
        readings.append({"ts": ts.isoformat(), "values": values(i), **({"state": state} if state else {})})
    with container.session_factory() as s:
        result = ingest_batch(s, TelemetryBatch.model_validate({"device_id": device_id, "readings": readings}),
                              container.settings)
        s.commit()
    assert result.accepted > 0


def jpeg_bytes(color=(40, 140, 60), size=(96, 96)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG")
    return buf.getvalue()
