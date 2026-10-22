"""ORM models for the sensor log, the shared knowledge store, diagnoses and chat."""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..config import get_settings
from .base import Base, BigIntPK, EmbeddingType, JSONType, UTCDateTime, utcnow

EMBEDDING_DIM = get_settings().embedding_dim


def _uuid() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# Sensor log (raw) and deterministic window aggregates
# ---------------------------------------------------------------------------


class SensorReading(Base):
    """One timestamped measurement from one sensor (long format, extensible)."""

    __tablename__ = "sensor_readings"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(sa.String(64))
    sensor: Mapped[str] = mapped_column(sa.String(32))
    ts: Mapped[datetime] = mapped_column(UTCDateTime())
    value: Mapped[float] = mapped_column(sa.Float)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    __table_args__ = (
        sa.UniqueConstraint("device_id", "sensor", "ts", name="uq_sensor_reading"),
        sa.Index("ix_sensor_readings_device_ts", "device_id", "ts"),
        sa.Index("ix_sensor_readings_sensor_ts", "sensor", "ts"),
        sa.Index("ix_sensor_readings_ts", "ts"),
    )


class SensorWindow(Base):
    """min / max / avg / std of one sensor over one fixed time window."""

    __tablename__ = "sensor_windows"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(sa.String(64))
    sensor: Mapped[str] = mapped_column(sa.String(32))
    window_start: Mapped[datetime] = mapped_column(UTCDateTime())
    window_end: Mapped[datetime] = mapped_column(UTCDateTime())
    count: Mapped[int] = mapped_column(sa.Integer)
    min_value: Mapped[float] = mapped_column(sa.Float)
    max_value: Mapped[float] = mapped_column(sa.Float)
    avg_value: Mapped[float] = mapped_column(sa.Float)
    std_value: Mapped[float] = mapped_column(sa.Float)
    first_value: Mapped[float] = mapped_column(sa.Float)
    last_value: Mapped[float] = mapped_column(sa.Float)
    first_ts: Mapped[datetime] = mapped_column(UTCDateTime())
    last_ts: Mapped[datetime] = mapped_column(UTCDateTime())
    min_ts: Mapped[datetime] = mapped_column(UTCDateTime())
    max_ts: Mapped[datetime] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        sa.UniqueConstraint("device_id", "sensor", "window_start", name="uq_sensor_window"),
        sa.Index("ix_sensor_windows_sensor_start", "sensor", "window_start"),
    )


class DirtyWindow(Base):
    """A window that received new readings and must be (re)aggregated and indexed."""

    __tablename__ = "dirty_windows"

    device_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    marked_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class SensorStateEvent(Base):
    """A sensor switch changed (on/off). Only changes are stored; intervals are rebuilt from them."""

    __tablename__ = "sensor_state_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(sa.String(64))
    toggle: Mapped[str] = mapped_column(sa.String(32))
    state: Mapped[str] = mapped_column(sa.String(3))  # on | off
    ts: Mapped[datetime] = mapped_column(UTCDateTime())

    __table_args__ = (
        sa.UniqueConstraint("device_id", "toggle", "ts", name="uq_sensor_state_event"),
        sa.Index("ix_sensor_state_device_toggle_ts", "device_id", "toggle", "ts"),
    )


class RoverHeartbeat(Base):
    """The rover was connected during this 5-minute bucket (a batch arrived, even one without readings)."""

    __tablename__ = "rover_heartbeats"

    device_id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime(), primary_key=True)
    last_seen: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------------------
# Shared knowledge store (read by Pipeline A, written by ingestion + Pipeline B)
# ---------------------------------------------------------------------------


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=_uuid)
    # sensor_window | sensor_daily | sensor_sessions | sensor_digest_llm | diagnosis | agent_finding
    # | farm_note | document
    source_type: Mapped[str] = mapped_column(sa.String(32))
    source_id: Mapped[str] = mapped_column(sa.String(160))
    chunk_index: Mapped[int] = mapped_column(sa.Integer, default=0)
    title: Mapped[str] = mapped_column(sa.String(300))
    content: Mapped[str] = mapped_column(sa.Text)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType(EMBEDDING_DIM), nullable=True)
    embed_model: Mapped[str] = mapped_column(sa.String(160))
    time_start: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    time_end: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)
    content_hash: Mapped[str] = mapped_column(sa.String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    __table_args__ = (
        sa.Index("ix_knowledge_source", "source_type", "source_id"),
        sa.Index("ix_knowledge_time", "time_start", "time_end"),
    )


class FarmDocument(Base):
    """A farmer-provided note or uploaded document (chunked into the knowledge store)."""

    __tablename__ = "farm_documents"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(sa.String(16))  # note | document
    title: Mapped[str] = mapped_column(sa.String(300))
    filename: Mapped[str | None] = mapped_column(sa.String(300), nullable=True)
    content: Mapped[str] = mapped_column(sa.Text)
    chunk_count: Mapped[int] = mapped_column(sa.Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ---------------------------------------------------------------------------
# Pipeline B records
# ---------------------------------------------------------------------------


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    source: Mapped[str] = mapped_column(sa.String(16))  # rover | phone | web | api
    note: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    image_path: Mapped[str] = mapped_column(sa.String(512))
    image_sha256: Mapped[str] = mapped_column(sa.String(64))
    # processing | completed | not_plant | failed
    status: Mapped[str] = mapped_column(sa.String(16), default="processing")
    # queued | vlm | searching | writing | done
    stage: Mapped[str] = mapped_column(sa.String(16), default="queued")
    vlm_model: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    agent_model: Mapped[str | None] = mapped_column(sa.String(160), nullable=True)
    analysis: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    findings: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    sources: Mapped[list] = mapped_column(JSONType, default=list)
    agent_trace: Mapped[list] = mapped_column(JSONType, default=list)
    web_grounded: Mapped[bool] = mapped_column(sa.Boolean, default=False)
    knowledge_chunk_ids: Mapped[list] = mapped_column(JSONType, default=list)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    timings: Mapped[dict] = mapped_column(JSONType, default=dict)

    __table_args__ = (sa.Index("ix_diagnoses_created", "created_at"),)


# ---------------------------------------------------------------------------
# Pipeline A conversation memory
# ---------------------------------------------------------------------------


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(sa.String(32), primary_key=True, default=_uuid)
    title: Mapped[str] = mapped_column(sa.String(200), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)

    messages: Mapped[list[ChatMessage]] = relationship(
        back_populates="session", order_by="ChatMessage.id", cascade="all, delete-orphan"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(sa.String(16))  # user | assistant
    mode: Mapped[str] = mapped_column(sa.String(16))  # farm | open
    content: Mapped[str] = mapped_column(sa.Text)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)

    session: Mapped[ChatSession] = relationship(back_populates="messages")

    __table_args__ = (sa.Index("ix_chat_messages_session", "session_id", "id"),)
