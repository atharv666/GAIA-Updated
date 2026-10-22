"""Live view API: a snapshot of what the system is doing and a Server-Sent Events stream of activity."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..container import Container
from ..core.activity import ACTIVITY
from ..core.timeutil import floor_window, get_tz
from ..db.base import utcnow
from ..db.models import KnowledgeChunk, SensorReading
from ..ingestion.coverage import switch_state
from .deps import get_container, get_db, require_api_key
from .telemetry import latest_readings

router = APIRouter(tags=["live"], dependencies=[Depends(require_api_key)])


@router.get("/live/state")
def live_state(
    events: int = Query(40, ge=0, le=200), s: Session = Depends(get_db), c: Container = Depends(get_container)
):
    """Everything the Live view needs to draw itself: current window progress, counters and recent events."""
    now = utcnow()
    minutes = c.settings.window_minutes
    start = floor_window(now, minutes, get_tz(c.settings.farm_timezone))
    end = start + timedelta(minutes=minutes)
    in_window = s.execute(
        sa.select(sa.func.count(sa.distinct(SensorReading.ts)), sa.func.count()).where(
            SensorReading.ts >= start, SensorReading.ts < end
        )
    ).one()
    by_type = dict(
        s.execute(sa.select(KnowledgeChunk.source_type, sa.func.count()).group_by(KnowledgeChunk.source_type)).all()
    )
    recent = ACTIVITY.since(0, limit=events) if events else []
    return {
        "server_time": now.isoformat(),
        "window_minutes": minutes,
        "current_window": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "seconds_left": max(0, int((end - now).total_seconds())),
            "readings": int(in_window[0]),
            "values": int(in_window[1]),
        },
        "latest": latest_readings(s),
        "sensor_switches": switch_state(s, now),
        "pending_windows": c.ingestion.pending(),
        "knowledge_by_type": by_type,
        "embedder": c.embedder.model_id,
        "llm": c.llm.model_id if c.llm.available else None,
        "counters": ACTIVITY.snapshot_counters(),
        "last_event_id": ACTIVITY.last_id,
        "events": [e.to_dict() for e in recent],
    }


@router.get("/live/stream")
async def live_stream(request: Request, after: int | None = Query(None, ge=0)):
    """Server-Sent Events: one ``activity`` event per thing the system does. ``after`` resumes from an id."""

    async def events() -> AsyncIterator[str]:
        last = ACTIVITY.last_id if after is None else after
        idle = 0.0
        yield f"event: hello\ndata: {json.dumps({'last_event_id': last})}\n\n"
        while not await request.is_disconnected():
            fresh = ACTIVITY.since(last)
            for e in fresh:
                last = e.id
                yield f"id: {e.id}\nevent: activity\ndata: {json.dumps(e.to_dict(), ensure_ascii=False, default=str)}\n\n"
            if fresh:
                idle = 0.0
            else:
                idle += 0.4
                if idle >= 15:  # keep the connection alive through proxies and mobile networks
                    idle = 0.0
                    yield ": ping\n\n"
            await asyncio.sleep(0.4)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
