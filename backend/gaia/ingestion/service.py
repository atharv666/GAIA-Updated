"""Ingestion orchestration: aggregate dirty windows, build documents, index them.

Runs continuously in the background scheduler, and on demand right before a
"Query my farm" question (``sync(include_open=True)``) so answers always see the
freshest readings, including the window that is still filling up.
"""

from __future__ import annotations

import logging
import statistics
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..core.activity import ACTIVITY
from ..core.timeutil import day_bounds, fmt_local, get_tz, local_date
from ..db.base import utcnow
from ..db.models import DirtyWindow, KnowledgeChunk, SensorReading, SensorWindow
from ..knowledge.store import KnowledgeStore
from ..providers.embeddings import Embedder
from ..providers.llm import LLM, LLMError
from .coverage import day_lines, sessions_document, window_reasons
from .documents import (
    daily_document,
    daily_source_id,
    llm_digest,
    window_document,
    window_source_id,
)

log = logging.getLogger(__name__)


@dataclass
class SyncReport:
    windows_aggregated: int = 0
    windows_indexed: int = 0
    open_windows: int = 0
    days_rolled_up: int = 0
    digests_written: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def aggregate_window(session: Session, device_id: str, window_start: datetime, minutes: int) -> list[SensorWindow]:
    """Recompute min/max/avg/std for every sensor in one window (idempotent)."""
    window_end = window_start + timedelta(minutes=minutes)
    rows = session.execute(
        sa.select(SensorReading.sensor, SensorReading.ts, SensorReading.value)
        .where(
            SensorReading.device_id == device_id,
            SensorReading.ts >= window_start,
            SensorReading.ts < window_end,
        )
        .order_by(SensorReading.ts)
    ).all()
    session.execute(
        sa.delete(SensorWindow).where(SensorWindow.device_id == device_id, SensorWindow.window_start == window_start)
    )
    grouped: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for sensor, ts, value in rows:
        grouped[sensor].append((ts, value))
    windows = []
    for sensor, points in grouped.items():
        values = [v for _, v in points]
        min_i = min(range(len(values)), key=values.__getitem__)
        max_i = max(range(len(values)), key=values.__getitem__)
        windows.append(
            SensorWindow(
                device_id=device_id,
                sensor=sensor,
                window_start=window_start,
                window_end=window_end,
                count=len(values),
                min_value=values[min_i],
                max_value=values[max_i],
                avg_value=statistics.fmean(values),
                std_value=statistics.pstdev(values) if len(values) > 1 else 0.0,
                first_value=values[0],
                last_value=values[-1],
                first_ts=points[0][0],
                last_ts=points[-1][0],
                min_ts=points[min_i][0],
                max_ts=points[max_i][0],
            )
        )
    session.add_all(windows)
    session.flush()
    return windows


class IngestionService:
    _lock = threading.Lock()

    def __init__(self, session_factory: sessionmaker[Session], embedder: Embedder, llm: LLM, settings: Settings):
        self.session_factory = session_factory
        self.embedder = embedder
        self.llm = llm
        self.settings = settings
        self.tz = get_tz(settings.farm_timezone)

    def sync(self, *, include_open: bool = False, now: datetime | None = None) -> SyncReport:
        """Process dirty windows.

        Closed windows (older than window length + grace) are aggregated,
        indexed as knowledge chunks and cleared. With ``include_open`` the
        still-filling windows are aggregated too (so statistics are current)
        but stay dirty and are indexed once they close.
        """
        now = now or utcnow()
        minutes = self.settings.window_minutes
        cutoff = now - timedelta(minutes=minutes, seconds=self.settings.ingest_grace_seconds)
        report = SyncReport()
        with self._lock, self.session_factory() as s:
            dirty = s.execute(
                sa.select(DirtyWindow.device_id, DirtyWindow.window_start, DirtyWindow.marked_at).order_by(
                    DirtyWindow.window_start
                )
            ).all()
            days: set[tuple[str, date]] = set()
            store = KnowledgeStore(s, self.embedder)
            for device_id, window_start, marked_at in dirty:
                closed = window_start <= cutoff
                if not closed and not include_open:
                    continue
                try:
                    windows = aggregate_window(s, device_id, window_start, minutes)
                    report.windows_aggregated += 1
                    if not closed:
                        report.open_windows += 1
                        continue
                    source_id = window_source_id(device_id, window_start)
                    ACTIVITY.publish(
                        "window_closed",
                        f"15-minute window {fmt_local(window_start, self.tz, '%H:%M')}-"
                        f"{fmt_local(window_start + timedelta(minutes=minutes), self.tz, '%H:%M')} closed: "
                        "minimum, maximum and average computed",
                        device_id=device_id, window_start=window_start.isoformat(),
                        window_end=(window_start + timedelta(minutes=minutes)).isoformat(),
                        sensors=sorted(w.sensor for w in windows), readings=max((w.count for w in windows), default=0),
                    )
                    ACTIVITY.count("windows")
                    reasons = window_reasons(
                        s, device_id, window_start, window_start + timedelta(minutes=minutes),
                        {w.sensor for w in windows},
                    )
                    doc = window_document(device_id, windows, self.tz, reasons)
                    store.upsert_source("sensor_window", source_id, [doc] if doc else [])
                    report.windows_indexed += 1
                    # Only clear the flag if no new readings arrived meanwhile.
                    s.execute(
                        sa.delete(DirtyWindow).where(
                            DirtyWindow.device_id == device_id,
                            DirtyWindow.window_start == window_start,
                            DirtyWindow.marked_at == marked_at,
                        )
                    )
                    days.add((device_id, local_date(window_start, self.tz)))
                    s.commit()
                except LLMError as exc:  # embedding provider failure: keep dirty, retry later
                    s.rollback()
                    report.errors.append(f"{device_id}@{window_start.isoformat()}: {exc}")
                    log.warning("Indexing window failed: %s", exc)
            s.commit()

            for device_id, day in sorted(days):
                try:
                    self._rollup_day(s, store, device_id, day, now, report)
                    s.commit()
                except LLMError as exc:
                    s.rollback()
                    report.errors.append(f"daily {device_id}@{day}: {exc}")
        return report

    def _rollup_day(
        self, s: Session, store: KnowledgeStore, device_id: str, day: date, now: datetime, report: SyncReport
    ) -> None:
        start, end = day_bounds(day, self.tz)
        windows = s.scalars(
            sa.select(SensorWindow).where(
                SensorWindow.device_id == device_id,
                SensorWindow.window_start >= start,
                SensorWindow.window_start < end,
            )
        ).all()
        # The rollup of a day only includes closed windows.
        cutoff = now - timedelta(minutes=self.settings.window_minutes, seconds=self.settings.ingest_grace_seconds)
        windows = [w for w in windows if w.window_start <= cutoff]
        doc = daily_document(device_id, day, windows, self.tz, day_lines(s, device_id, day, self.tz, now))
        store.upsert_source("sensor_daily", daily_source_id(device_id, day), [doc] if doc else [])
        sessions = sessions_document(s, device_id, day, self.tz, now)
        store.upsert_source("sensor_sessions", daily_source_id(device_id, day), [sessions] if sessions else [])
        report.days_rolled_up += 1
        day_complete = end <= now
        if doc and day_complete and self.settings.llm_digest_enabled and self.llm.available:
            self._write_digest(s, store, device_id, day, doc, report)

    def _write_digest(self, s, store, device_id, day, daily, report) -> None:
        source_id = daily_source_id(device_id, day)
        existing = s.scalar(
            sa.select(KnowledgeChunk).where(
                KnowledgeChunk.source_type == "sensor_digest_llm", KnowledgeChunk.source_id == source_id
            )
        )
        if existing and (existing.meta or {}).get("rollup_hash") == daily.content_hash:
            return
        digest = llm_digest(self.llm, daily, device_id, day)
        store.upsert_source("sensor_digest_llm", source_id, [digest])
        report.digests_written += 1

    def build_digest(self, device_id: str, day: date) -> dict:
        """Force (re)generation of the LLM digest for one day."""
        with self.session_factory() as s:
            store = KnowledgeStore(s, self.embedder)
            daily = s.scalar(
                sa.select(KnowledgeChunk).where(
                    KnowledgeChunk.source_type == "sensor_daily",
                    KnowledgeChunk.source_id == daily_source_id(device_id, day),
                )
            )
            if daily is None:
                raise LookupError(f"No daily summary for {device_id} on {day}")
            from ..knowledge.store import ChunkDraft

            draft = ChunkDraft(daily.title, daily.content, daily.time_start, daily.time_end, daily.meta)
            digest = llm_digest(self.llm, draft, device_id, day)
            ids = store.upsert_source("sensor_digest_llm", daily_source_id(device_id, day), [digest])
            s.commit()
            return {"chunk_ids": ids, "content": digest.content}

    def pending(self) -> int:
        with self.session_factory() as s:
            return int(s.scalar(sa.select(sa.func.count()).select_from(DirtyWindow)) or 0)
