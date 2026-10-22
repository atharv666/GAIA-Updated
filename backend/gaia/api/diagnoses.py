"""Pipeline B diagnosis API (asynchronous job with stage polling)."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..container import Container
from ..db.models import Diagnosis
from ..knowledge.store import KnowledgeStore
from ..pipeline_b.service import UploadTooLarge
from ..pipeline_b.vlm import InvalidImage
from ..rover.client import RoverUnavailable
from .deps import get_container, get_db, require_api_key

router = APIRouter(tags=["diagnosis"], dependencies=[Depends(require_api_key)])


def _require_vlm(c: Container) -> None:
    if not c.vlm.available:
        raise HTTPException(503, "No vision-language model configured. Set GEMINI_API_KEY (or a vision-capable "
                                 "OPENAI_COMPAT model) in backend/.env")


def _start(c: Container, s: Session, data: bytes, source: str, note: str | None, wait: bool, bg: BackgroundTasks):
    _require_vlm(c)
    try:
        d = c.diagnosis.create(s, data, source, note)
    except UploadTooLarge as exc:
        raise HTTPException(413, str(exc)) from exc
    except InvalidImage as exc:
        raise HTTPException(422, str(exc)) from exc
    if wait:
        c.diagnosis.run(d.id)
        s.refresh(d)
        return c.diagnosis.to_dict(d)
    bg.add_task(c.diagnosis.run, d.id)
    return JSONResponse(c.diagnosis.to_dict(d), status_code=202)


@router.post("/diagnoses")
def create_diagnosis(
    bg: BackgroundTasks,
    image: UploadFile = File(..., description="Crop photo (JPEG/PNG/WebP)"),
    source: str = Form("phone", description="rover | phone | web | api"),
    note: str | None = Form(None, description="Optional location/context label, stored only"),
    wait: bool = Query(False, description="Block until the pipeline finishes"),
    s: Session = Depends(get_db),
    c: Container = Depends(get_container),
):
    """Upload a crop photo. Returns 202 with a job to poll (GET /diagnoses/{id}), or the result with wait=true."""
    data = image.file.read(c.settings.max_upload_mb * 1024 * 1024 + 1)  # the service rejects oversize
    return _start(c, s, data, source, note, wait, bg)


class RoverCaptureRequest(BaseModel):
    note: str | None = Field(default=None, max_length=500)


@router.post("/diagnoses/from-rover")
def diagnose_from_rover(
    req: RoverCaptureRequest,
    bg: BackgroundTasks,
    wait: bool = Query(False),
    s: Session = Depends(get_db),
    c: Container = Depends(get_container),
):
    """Ask the rover for a high-resolution still and run Pipeline B on it."""
    _require_vlm(c)  # fail fast before driving the camera
    try:
        data = c.rover.capture()
    except RoverUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc
    return _start(c, s, data, "rover", req.note, wait, bg)


@router.get("/diagnoses")
def list_diagnoses(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    s: Session = Depends(get_db),
    c: Container = Depends(get_container),
):
    return [c.diagnosis.summary(d) for d in c.diagnosis.list(s, limit, offset)]


def _get(s: Session, diagnosis_id: str) -> Diagnosis:
    d = s.get(Diagnosis, diagnosis_id)
    if d is None:
        raise HTTPException(404, "Diagnosis not found")
    return d


@router.get("/diagnoses/{diagnosis_id}")
def get_diagnosis(diagnosis_id: str, s: Session = Depends(get_db), c: Container = Depends(get_container)):
    return c.diagnosis.to_dict(_get(s, diagnosis_id))


@router.get("/diagnoses/{diagnosis_id}/image")
def diagnosis_image(diagnosis_id: str, s: Session = Depends(get_db), c: Container = Depends(get_container)):
    path = c.diagnosis.image_path(_get(s, diagnosis_id))
    if not path.exists():
        raise HTTPException(404, "Image file missing")
    return FileResponse(path, media_type="image/jpeg")


@router.delete("/diagnoses/{diagnosis_id}")
def delete_diagnosis(diagnosis_id: str, s: Session = Depends(get_db), c: Container = Depends(get_container)):
    """Delete a diagnosis, its image and the knowledge it wrote."""
    d = _get(s, diagnosis_id)
    store = KnowledgeStore(s, c.embedder)
    removed = store.delete_source("diagnosis", d.id) + store.delete_source("agent_finding", d.id)
    path = c.diagnosis.image_path(d)
    s.delete(d)
    s.commit()
    path.unlink(missing_ok=True)
    return {"deleted": diagnosis_id, "knowledge_chunks_removed": removed}
