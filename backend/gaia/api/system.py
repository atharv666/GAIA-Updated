"""Health, public configuration and ingestion control."""

from __future__ import annotations

from datetime import date

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import __version__
from ..container import Container
from ..core.sensors import public_registry
from ..db.models import Diagnosis, KnowledgeChunk, SensorReading, SensorWindow
from ..knowledge.store import KnowledgeStore
from ..providers.llm import LLMError, LLMUnavailable
from .deps import get_container, get_db, require_api_key

router = APIRouter(tags=["system"])


@router.get("/health")
def health(s: Session = Depends(get_db), c: Container = Depends(get_container)):
    try:
        s.execute(sa.text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "ok" if db_ok else "degraded", "version": __version__, "db": db_ok, **c.describe()}


@router.get("/config", dependencies=[Depends(require_api_key)])
def public_config(c: Container = Depends(get_container)):
    st = c.settings
    return {
        "app_name": st.app_name,
        "farm_name": st.farm_name,
        "farmer_name": st.farmer_name,
        "timezone": st.farm_timezone,
        "region": st.farm_region,
        "window_minutes": st.window_minutes,
        "sensors": public_registry(),
        "rover": {"base_url": c.rover.base_url, "stream_url": c.rover.stream_url},
        "providers": c.describe(),
    }


@router.get("/overview", dependencies=[Depends(require_api_key)])
def overview(s: Session = Depends(get_db), c: Container = Depends(get_container)):
    """Counts for dashboards."""
    last_ts = s.scalar(sa.select(sa.func.max(SensorReading.ts)))
    return {
        "readings": s.scalar(sa.select(sa.func.count()).select_from(SensorReading)) or 0,
        "windows": s.scalar(sa.select(sa.func.count()).select_from(SensorWindow)) or 0,
        "last_reading_at": last_ts.isoformat() if last_ts else None,
        "diagnoses": s.scalar(sa.select(sa.func.count()).select_from(Diagnosis)) or 0,
        "knowledge": KnowledgeStore(s, c.embedder).stats(),
        "pending_windows": c.ingestion.pending(),
        "latest_diagnoses": [c.diagnosis.summary(d) for d in c.diagnosis.list(s, 3)],
    }


class SyncRequest(BaseModel):
    include_open: bool = False


@router.post("/ingest/sync", dependencies=[Depends(require_api_key)])
def ingest_sync(req: SyncRequest | None = None, c: Container = Depends(get_container)):
    """Aggregate and index pending sensor windows now."""
    return c.ingestion.sync(include_open=bool(req and req.include_open)).to_dict()


@router.post("/ingest/reindex", dependencies=[Depends(require_api_key)])
def ingest_reindex(s: Session = Depends(get_db), c: Container = Depends(get_container)):
    """Re-embed every chunk with the current embedding model (after changing providers)."""
    try:
        n = KnowledgeStore(s, c.embedder).reindex_all()
    except LLMError as exc:
        s.rollback()
        raise HTTPException(502, str(exc)) from exc
    s.commit()
    return {"reindexed": n, "embedder": c.embedder.model_id}


class DigestRequest(BaseModel):
    device_id: str
    date: date


@router.post("/ingest/digest", dependencies=[Depends(require_api_key)])
def ingest_digest(req: DigestRequest, c: Container = Depends(get_container)):
    """Generate the optional LLM interpretation for one day's sensor summary."""
    try:
        return c.ingestion.build_digest(req.device_id, req.date)
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except LLMUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except LLMError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/ingest/status", dependencies=[Depends(require_api_key)])
def ingest_status(s: Session = Depends(get_db), c: Container = Depends(get_container)):
    by_type = dict(
        s.execute(sa.select(KnowledgeChunk.source_type, sa.func.count()).group_by(KnowledgeChunk.source_type)).all()
    )
    return {"pending_windows": c.ingestion.pending(), "knowledge_by_type": by_type}
