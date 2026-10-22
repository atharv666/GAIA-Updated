"""Index farmer notes and uploaded documents into the shared knowledge store."""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..core.chunking import chunk_text
from ..db.models import FarmDocument
from ..providers.embeddings import Embedder
from .store import ChunkDraft, KnowledgeStore


def source_type_for(doc: FarmDocument) -> str:
    return "farm_note" if doc.kind == "note" else "document"


def index_farm_document(s: Session, embedder: Embedder, doc: FarmDocument) -> list[str]:
    """Chunk + embed a note/document (undated, so it is visible to every time-filtered query)."""
    pieces = chunk_text(doc.content)
    drafts = [
        ChunkDraft(
            title=doc.title if len(pieces) == 1 else f"{doc.title} (part {i}/{len(pieces)})",
            content=piece,
            meta={
                "document_id": doc.id,
                "kind": doc.kind,
                "filename": doc.filename,
                "created_at": doc.created_at.isoformat() if doc.created_at else None,
            },
        )
        for i, piece in enumerate(pieces, start=1)
    ]
    ids = KnowledgeStore(s, embedder).upsert_source(source_type_for(doc), doc.id, drafts)
    doc.chunk_count = len(ids)
    return ids
