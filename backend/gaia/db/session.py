"""Engine creation (external PostgreSQL, embedded PostgreSQL, or SQLite) and schema setup."""

from __future__ import annotations

import logging
import time

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from . import models  # noqa: F401  (register tables on Base.metadata)
from .base import Base

log = logging.getLogger(__name__)

_embedded_server = None  # keeps the pgserver process alive for the app lifetime


def resolve_database_url(settings: Settings) -> str:
    url = settings.database_url
    if url == "embedded":
        url = _start_embedded_postgres(settings)
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            url = "postgresql+psycopg://" + url[len(prefix):]
    return url


def _start_embedded_postgres(settings: Settings) -> str:
    global _embedded_server
    try:
        import pgserver
    except ImportError as exc:  # pragma: no cover - depends on local install
        raise RuntimeError(
            "DATABASE_URL=embedded needs the 'pgserver' package "
            "(pip install -r requirements-local.txt), or point DATABASE_URL at a "
            "PostgreSQL server with pgvector (see docker-compose.yml)."
        ) from exc
    if _embedded_server is not None:
        return _embedded_server.get_uri()
    pgdata = settings.data_dir / "pgdata"
    pgdata.parent.mkdir(parents=True, exist_ok=True)
    # After an unclean shutdown (killed process, closed terminal) two things can happen:
    # pgserver attaches to a postmaster that is still exiting, or Postgres needs crash
    # recovery, which on Windows can stall ~30 s and make pgserver's pg_ctl call time out
    # while recovery carries on in the background. Keep retrying until a connection works.
    deadline = time.monotonic() + 120
    attempt = 0
    while True:
        attempt += 1
        server = None
        try:
            server = pgserver.get_server(pgdata, cleanup_mode="stop")
            uri = server.get_uri()
            if _can_connect(uri):
                _embedded_server = server
                log.info("Embedded PostgreSQL running at %s", uri)
                return uri
            log.warning("Embedded PostgreSQL at %s is not accepting connections yet (attempt %d)", uri, attempt)
        except Exception as exc:  # pgserver raises assorted errors while a server is recovering
            log.warning("Embedded PostgreSQL not ready (attempt %d): %s", attempt, str(exc)[:200])
        # Drop the cached handle so the next attempt re-reads postmaster.pid.
        pgserver.PostgresServer._instances.pop(pgdata.expanduser().resolve(), None)
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"Embedded PostgreSQL in {pgdata} did not become available. If a previous backend "
                "crashed, wait a minute and start again, or delete that folder to start fresh."
            )
        time.sleep(min(10, 2 + attempt))


def _can_connect(uri: str) -> bool:
    import psycopg

    try:
        with psycopg.connect(uri, connect_timeout=5) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.OperationalError:
        return False


def create_engine(settings: Settings) -> sa.Engine:
    url = resolve_database_url(settings)
    if url.startswith("sqlite"):
        engine = sa.create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.close()

        return engine
    return sa.create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def make_session_factory(engine: sa.Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_db(engine: sa.Engine) -> None:
    """Create extension, tables and the vector / full-text indexes (idempotent)."""
    is_pg = engine.dialect.name == "postgresql"
    if is_pg:
        with engine.begin() as conn:
            conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    if is_pg:
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "CREATE INDEX IF NOT EXISTS ix_knowledge_embedding_hnsw "
                    "ON knowledge_chunks USING hnsw (embedding vector_cosine_ops)"
                )
            )
            conn.execute(
                sa.text(
                    "CREATE INDEX IF NOT EXISTS ix_knowledge_fts ON knowledge_chunks "
                    f"USING gin ({FTS_DOCUMENT_SQL})"
                )
            )


# Shared by the index definition above and the lexical retriever so that the
# planner can use the GIN index.
FTS_DOCUMENT_SQL = "to_tsvector('english'::regconfig, coalesce(title, '') || ' ' || content)"
