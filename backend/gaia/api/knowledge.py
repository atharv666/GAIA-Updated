"""Shared knowledge store API: browse, inspect retrieval, add farmer notes and documents."""

from __future__ import annotations

from io import BytesIO

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..container import Container
from ..db.models import FarmDocument
from ..knowledge.documents import index_farm_document, source_type_for
from ..knowledge.store import ALL_SOURCE_TYPES, KnowledgeStore, chunk_to_dict
from ..providers.llm import LLMError
from .deps import get_container, get_db, require_api_key

router = APIRouter(tags=["knowledge"], dependencies=[Depends(require_api_key)])

MAX_DOCUMENT_BYTES = 10 * 1024 * 1024


@router.get("/knowledge")
def list_chunks(
    source_type: str | None = Query(None, description=" | ".join(ALL_SOURCE_TYPES)),
    limit: int = Query(30, ge=1, le=200),
    offset: int = Query(0, ge=0),
    s: Session = Depends(get_db),
    c: Container = Depends(get_container),
):
    return [chunk_to_dict(ch) for ch in KnowledgeStore(s, c.embedder).list_chunks(source_type, limit, offset)]


@router.get("/knowledge/stats")
def stats(s: Session = Depends(get_db), c: Container = Depends(get_container)):
    return KnowledgeStore(s, c.embedder).stats()


@router.get("/knowledge/search")
def search(
    q: str = Query(..., min_length=1, max_length=500),
    k: int = Query(8, ge=1, le=30),
    source_type: list[str] | None = Query(None),
    s: Session = Depends(get_db),
    c: Container = Depends(get_container),
):
    """Inspect hybrid retrieval: fused score, vector similarity and lexical rank per chunk."""
    try:
        hits = KnowledgeStore(s, c.embedder).search(q, k=k, source_types=source_type)
    except LLMError as exc:
        raise HTTPException(502, f"Embedding provider error: {exc}") from exc
    return {"query": q, "embedder": c.embedder.model_id, "results": [h.to_dict() for h in hits]}


def _index_document(s: Session, c: Container, doc: FarmDocument) -> list[str]:
    try:
        return index_farm_document(s, c.embedder, doc)
    except LLMError as exc:
        s.rollback()
        raise HTTPException(502, f"Embedding provider error: {exc}") from exc


class NoteIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=20000)


@router.post("/knowledge/notes")
def add_note(note: NoteIn, s: Session = Depends(get_db), c: Container = Depends(get_container)):
    """Farmer notes, e.g. 'Planted tomato (var. Arka Rakshak) in the north plot on 2 Sep'."""
    doc = FarmDocument(kind="note", title=note.title.strip(), content=note.text.strip())
    s.add(doc)
    s.flush()
    ids = _index_document(s, c, doc)
    s.commit()
    return {"id": doc.id, "chunk_ids": ids}


def _extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        try:
            reader = PdfReader(BytesIO(data))
            return "\n\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            raise HTTPException(422, f"Could not read PDF: {exc}") from exc
    if name.endswith((".txt", ".md", ".csv", ".markdown")):
        return data.decode("utf-8", errors="replace")
    raise HTTPException(415, "Supported document types: .pdf, .txt, .md, .csv")


@router.post("/knowledge/documents")
def upload_document(
    file: UploadFile = File(...),
    title: str | None = Form(None),
    s: Session = Depends(get_db),
    c: Container = Depends(get_container),
):
    data = file.file.read(MAX_DOCUMENT_BYTES + 1)
    if len(data) > MAX_DOCUMENT_BYTES:
        raise HTTPException(413, "Document larger than 10 MB")
    text = _extract_text(file.filename or "upload.txt", data)
    if not text.strip():
        raise HTTPException(422, "No extractable text in document")
    doc = FarmDocument(kind="document", title=(title or file.filename or "Document").strip()[:200],
                       filename=file.filename, content=text)
    s.add(doc)
    s.flush()
    ids = _index_document(s, c, doc)
    s.commit()
    return {"id": doc.id, "chunk_ids": ids, "characters": len(text)}


@router.get("/knowledge/documents")
def list_documents(s: Session = Depends(get_db)):
    rows = s.scalars(sa.select(FarmDocument).order_by(FarmDocument.created_at.desc()).limit(100)).all()
    return [
        {"id": d.id, "kind": d.kind, "title": d.title, "filename": d.filename, "chunk_count": d.chunk_count,
         "created_at": d.created_at.isoformat(), "preview": d.content[:200]}
        for d in rows
    ]


@router.delete("/knowledge/documents/{document_id}")
def delete_document(document_id: str, s: Session = Depends(get_db), c: Container = Depends(get_container)):
    doc = s.get(FarmDocument, document_id)
    if doc is None:
        raise HTTPException(404, "Document not found")
    store = KnowledgeStore(s, c.embedder)
    removed = store.delete_source(source_type_for(doc), doc.id)
    s.delete(doc)
    s.commit()
    return {"deleted": document_id, "knowledge_chunks_removed": removed}
