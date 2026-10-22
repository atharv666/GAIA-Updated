"""Integration tests on real PostgreSQL + pgvector (embedded via pgserver). Skipped if unavailable."""

from datetime import datetime

import pytest
import sqlalchemy as sa

from gaia.config import Settings
from gaia.container import Container
from gaia.db.base import Base
from gaia.db.models import KnowledgeChunk
from gaia.knowledge.store import ChunkDraft, KnowledgeStore
from gaia.providers.embeddings import HashingEmbedder

from .conftest import FIXED_NOW, IST, FakeSearch, ScriptedLLM, seed_series

pgserver = pytest.importorskip("pgserver")
pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def pg_url(tmp_path_factory):
    server = pgserver.get_server(tmp_path_factory.mktemp("pg"), cleanup_mode="stop")
    yield server.get_uri().replace("postgresql://", "postgresql+psycopg://")


@pytest.fixture
def pg_container(pg_url, tmp_path):
    engine = sa.create_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.drop_all(engine)
    settings = Settings(
        database_url=pg_url, data_dir=tmp_path, scheduler_enabled=False, embedding_provider="hashing",
        search_provider="none", gemini_api_key=None, openai_compat_api_key=None, device_key="k",
    )
    c = Container.build(settings, engine=engine, llm=ScriptedLLM(), vlm=ScriptedLLM(),
                        embedder=HashingEmbedder(settings.embedding_dim), search=FakeSearch())
    yield c
    engine.dispose()


def test_schema_has_vector_and_indexes(pg_container):
    with pg_container.engine.connect() as conn:
        coltype = conn.execute(sa.text(
            "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'knowledge_chunks'::regclass AND attname = 'embedding'")).scalar()
        indexes = {r[0] for r in conn.execute(sa.text(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'knowledge_chunks'"))}
    assert coltype == "vector(768)"
    assert {"ix_knowledge_embedding_hnsw", "ix_knowledge_fts"} <= indexes


def test_ingestion_and_hybrid_search_on_pgvector(pg_container):
    c = pg_container
    seed_series(c, start_local=datetime(2025, 6, 15, 6, 0, tzinfo=IST), minutes=30,
                values=lambda i: {"soil_moisture": 30 + i, "mq5": 480})
    # Duplicate batch is ignored by ON CONFLICT DO NOTHING ... RETURNING.
    with pytest.raises(AssertionError):
        seed_series(c, start_local=datetime(2025, 6, 15, 6, 0, tzinfo=IST), minutes=30,
                    values=lambda i: {"soil_moisture": 30 + i, "mq5": 480})
    report = c.ingestion.sync(now=FIXED_NOW)
    assert report.windows_indexed == 2 and not report.errors

    with c.session_factory() as s:
        store = KnowledgeStore(s, c.embedder)
        store.upsert_source("farm_note", "n1", [ChunkDraft("Biogas plant", "The biogas digester sits next to the "
                                                                          "cattle shed in the east corner.")])
        s.commit()
        hits = store.search("where is the biogas digester", k=3)
        assert hits[0].source_id == "n1"
        assert hits[0].vector_rank == 1 and hits[0].lexical_rank == 1
        # Methane band text from the MQ5 window documents is found lexically and semantically.
        gas = store.search("elevated methane signal", k=5, source_types=["sensor_window"])
        assert gas and all(h.source_type == "sensor_window" for h in gas)
        assert "elevated" in gas[0].content
        # Time filter excludes windows outside the range.
        none = store.search("soil moisture", k=5, source_types=["sensor_window"],
                            time_start=datetime(2025, 6, 16, 0, 0, tzinfo=IST),
                            time_end=datetime(2025, 6, 17, 0, 0, tzinfo=IST))
        assert none == []
        assert s.scalar(sa.select(sa.func.count()).select_from(KnowledgeChunk)) == 4  # 2 windows, 1 day, 1 note


def test_farm_answer_on_postgres(pg_container):
    c = pg_container
    seed_series(c, start_local=datetime(2025, 6, 15, 6, 0, tzinfo=IST), minutes=60,
                values=lambda i: {"humidity": 86, "temperature": 22})
    c.llm.handlers["GroundedAnswer"] = lambda sys, turns: {"answer": "Very humid [F1].", "cited": ["F1"]}
    with c.session_factory() as s:
        result = c.chat.farm.answer(s, "How humid was my field this morning?", [], now=FIXED_NOW)
    assert result.sources[0].id == "F1"
    assert "average 86%" in result.sources[0].snippet
    assert "'very humid' band" in result.sources[0].snippet
