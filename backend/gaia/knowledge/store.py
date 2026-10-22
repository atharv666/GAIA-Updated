"""Shared knowledge store: chunk upserts and hybrid (vector + lexical) retrieval.

Retrieval = pgvector cosine search  +  PostgreSQL full-text search, fused with
Reciprocal Rank Fusion. Vector search captures meaning ("dry soil" ~ "low soil
moisture"); lexical search captures exact tokens ("MQ5", "early blight").
On SQLite both halves run in Python so the test-suite needs no database server.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import Session

from ..core.activity import ACTIVITY, SOURCE_LABELS
from ..db.models import EMBEDDING_DIM, KnowledgeChunk
from ..db.session import FTS_DOCUMENT_SQL
from ..providers.embeddings import Embedder, tokenize

RRF_K = 60

SENSOR_SOURCE_TYPES = ("sensor_window", "sensor_daily", "sensor_sessions", "sensor_digest_llm")
DIAGNOSIS_SOURCE_TYPES = ("diagnosis", "agent_finding")
FARMER_SOURCE_TYPES = ("farm_note", "document")
ALL_SOURCE_TYPES = SENSOR_SOURCE_TYPES + DIAGNOSIS_SOURCE_TYPES + FARMER_SOURCE_TYPES


@dataclass
class ChunkDraft:
    title: str
    content: str
    time_start: datetime | None = None
    time_end: datetime | None = None
    meta: dict = field(default_factory=dict)

    @property
    def embed_text(self) -> str:
        return f"{self.title}\n{self.content}"

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.embed_text.encode()).hexdigest()


@dataclass
class ScoredChunk:
    id: str
    source_type: str
    source_id: str
    title: str
    content: str
    time_start: datetime | None
    time_end: datetime | None
    meta: dict
    score: float
    vector_similarity: float | None = None
    lexical_score: float | None = None
    vector_rank: int | None = None
    lexical_rank: int | None = None

    @classmethod
    def of(cls, c: KnowledgeChunk) -> ScoredChunk:
        return cls(
            id=c.id, source_type=c.source_type, source_id=c.source_id, title=c.title, content=c.content,
            time_start=c.time_start, time_end=c.time_end, meta=dict(c.meta or {}), score=0.0,
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "source_id": self.source_id,
            "title": self.title,
            "content": self.content,
            "time_start": self.time_start.isoformat() if self.time_start else None,
            "time_end": self.time_end.isoformat() if self.time_end else None,
            "meta": self.meta,
            "score": round(self.score, 5),
            "vector_similarity": None if self.vector_similarity is None else round(self.vector_similarity, 4),
            "lexical_score": None if self.lexical_score is None else round(self.lexical_score, 4),
            "vector_rank": self.vector_rank,
            "lexical_rank": self.lexical_rank,
        }


def chunk_to_dict(c: KnowledgeChunk) -> dict:
    return {
        "id": c.id,
        "source_type": c.source_type,
        "source_id": c.source_id,
        "chunk_index": c.chunk_index,
        "title": c.title,
        "content": c.content,
        "time_start": c.time_start.isoformat() if c.time_start else None,
        "time_end": c.time_end.isoformat() if c.time_end else None,
        "meta": c.meta or {},
        "embed_model": c.embed_model,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }


class KnowledgeStore:
    def __init__(self, session: Session, embedder: Embedder):
        self.s = session
        self.embedder = embedder
        self.is_pg = session.get_bind().dialect.name == "postgresql"

    # ------------------------------------------------------------------ write

    def upsert_source(self, source_type: str, source_id: str, drafts: list[ChunkDraft]) -> list[str]:
        """Replace all chunks of one logical source. Unchanged content is not re-embedded."""
        existing = self.s.scalars(
            sa.select(KnowledgeChunk)
            .where(KnowledgeChunk.source_type == source_type, KnowledgeChunk.source_id == source_id)
            .order_by(KnowledgeChunk.chunk_index)
        ).all()
        model_id = self.embedder.model_id
        if (
            existing
            and [c.content_hash for c in existing] == [d.content_hash for d in drafts]
            and all(c.embed_model == model_id for c in existing)
        ):
            for c, d in zip(existing, drafts):
                c.meta, c.time_start, c.time_end = d.meta, d.time_start, d.time_end
            return [c.id for c in existing]

        for c in existing:
            self.s.delete(c)
        self.s.flush()
        if not drafts:
            return []
        label = SOURCE_LABELS.get(source_type, source_type)
        first = drafts[0]
        ACTIVITY.publish(
            "chunk_created", f"Wrote a {label} in plain text",
            source_type=source_type, source_id=source_id, chunks=len(drafts), chunk_title=first.title,
            preview=first.content[:700], chars=sum(len(d.content) for d in drafts),
        )
        vectors = self.embedder.embed_documents([d.embed_text for d in drafts])
        ACTIVITY.publish(
            "chunk_embedded", f"Turned the {label} into {len(vectors[0])} numbers so it can be searched by meaning",
            source_type=source_type, model=model_id, dims=len(vectors[0]), chunks=len(drafts),
        )
        chunks = [
            KnowledgeChunk(
                source_type=source_type,
                source_id=source_id,
                chunk_index=i,
                title=d.title[:300],
                content=d.content,
                embedding=vec,
                embed_model=model_id,
                time_start=d.time_start,
                time_end=d.time_end,
                meta=d.meta,
                content_hash=d.content_hash,
            )
            for i, (d, vec) in enumerate(zip(drafts, vectors))
        ]
        self.s.add_all(chunks)
        self.s.flush()
        ACTIVITY.publish(
            "chunk_stored", f"Saved the {label} to the knowledge store",
            source_type=source_type, source_id=source_id, chunks=len(chunks), chunk_title=first.title,
        )
        ACTIVITY.count(f"chunks:{source_type}", len(chunks))
        return [c.id for c in chunks]

    def delete_source(self, source_type: str, source_id: str) -> int:
        result = self.s.execute(
            sa.delete(KnowledgeChunk).where(
                KnowledgeChunk.source_type == source_type, KnowledgeChunk.source_id == source_id
            )
        )
        return result.rowcount or 0

    def reindex_all(self, batch_size: int = 64) -> int:
        """Re-embed every chunk with the current embedder (after a model change)."""
        ids = self.s.scalars(sa.select(KnowledgeChunk.id).order_by(KnowledgeChunk.created_at)).all()
        for i in range(0, len(ids), batch_size):
            chunks = self.s.scalars(sa.select(KnowledgeChunk).where(KnowledgeChunk.id.in_(ids[i : i + batch_size]))).all()
            vectors = self.embedder.embed_documents([f"{c.title}\n{c.content}" for c in chunks])
            for c, v in zip(chunks, vectors):
                c.embedding, c.embed_model = v, self.embedder.model_id
            self.s.flush()
        return len(ids)

    # ------------------------------------------------------------------- read

    def _conditions(
        self, source_types: list[str] | tuple[str, ...] | None, time_start: datetime | None, time_end: datetime | None
    ) -> list:
        conds = []
        if source_types:
            conds.append(KnowledgeChunk.source_type.in_(list(source_types)))
        if time_start is not None and time_end is not None:
            # Keep undated chunks (notes, documents); dated chunks must overlap the range.
            conds.append(
                sa.or_(
                    KnowledgeChunk.time_start.is_(None),
                    sa.and_(
                        KnowledgeChunk.time_start < time_end,
                        sa.func.coalesce(KnowledgeChunk.time_end, KnowledgeChunk.time_start) >= time_start,
                    ),
                )
            )
        return conds

    def search(
        self,
        query: str,
        *,
        k: int = 6,
        source_types: list[str] | tuple[str, ...] | None = None,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
    ) -> list[ScoredChunk]:
        if not query.strip():
            return []
        conds = self._conditions(source_types, time_start, time_end)
        pool = max(k * 4, 24)
        vector_hits = self._vector_search(self.embedder.embed_query(query), conds, pool)
        lexical_hits = self._lexical_search(query, conds, pool)

        fused: dict[str, ScoredChunk] = {}
        for rank, (chunk, sim) in enumerate(vector_hits, start=1):
            item = fused.setdefault(chunk.id, ScoredChunk.of(chunk))
            item.vector_similarity, item.vector_rank = sim, rank
            item.score += 1.0 / (RRF_K + rank)
        for rank, (chunk, score) in enumerate(lexical_hits, start=1):
            item = fused.setdefault(chunk.id, ScoredChunk.of(chunk))
            item.lexical_score, item.lexical_rank = score, rank
            item.score += 1.0 / (RRF_K + rank)
        return sorted(fused.values(), key=lambda c: c.score, reverse=True)[:k]

    def _vector_search(self, qv: list[float], conds: list, limit: int) -> list[tuple[KnowledgeChunk, float]]:
        model_filter = [KnowledgeChunk.embed_model == self.embedder.model_id, KnowledgeChunk.embedding.is_not(None)]
        if self.is_pg:
            param = sa.cast(sa.bindparam("qv", qv, type_=Vector(EMBEDDING_DIM)), Vector(EMBEDDING_DIM))
            distance = KnowledgeChunk.embedding.op("<=>", return_type=sa.Float)(param)
            rows = self.s.execute(
                sa.select(KnowledgeChunk, distance.label("d")).where(*conds, *model_filter).order_by(distance).limit(limit)
            ).all()
            return [(row[0], 1.0 - float(row[1])) for row in rows]
        chunks = self.s.scalars(sa.select(KnowledgeChunk).where(*conds, *model_filter)).all()
        if not chunks:
            return []
        matrix = np.asarray([c.embedding for c in chunks], dtype=np.float64)
        sims = matrix @ np.asarray(qv, dtype=np.float64)
        order = np.argsort(-sims)[:limit]
        return [(chunks[i], float(sims[i])) for i in order]

    def _lexical_search(self, query: str, conds: list, limit: int) -> list[tuple[KnowledgeChunk, float]]:
        if self.is_pg:
            document = sa.literal_column(FTS_DOCUMENT_SQL)
            tsquery = sa.func.websearch_to_tsquery(sa.literal_column("'english'::regconfig"), query)
            # OR the terms together: farmer questions rarely contain every indexed word.
            or_query = sa.func.to_tsquery(
                sa.literal_column("'english'::regconfig"),
                sa.func.replace(sa.cast(tsquery, sa.Text), " & ", " | "),
            )
            rank = sa.func.ts_rank_cd(document, or_query)
            rows = self.s.execute(
                sa.select(KnowledgeChunk, rank.label("r"))
                .where(*conds, document.op("@@")(or_query))
                .order_by(rank.desc())
                .limit(limit)
            ).all()
            return [(row[0], float(row[1])) for row in rows]

        q_tokens = set(tokenize(query))
        if not q_tokens:
            return []
        chunks = self.s.scalars(sa.select(KnowledgeChunk).where(*conds)).all()
        docs = [(c, Counter(tokenize(f"{c.title} {c.content}"))) for c in chunks]
        n = len(docs)
        df = Counter(t for _, tf in docs for t in q_tokens if t in tf)
        scored = []
        for c, tf in docs:
            s = sum(
                math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5)) * tf[t] / (tf[t] + 1.2)
                for t in q_tokens
                if t in tf
            )
            if s > 0:
                scored.append((c, s))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def list_chunks(self, source_type: str | None = None, limit: int = 50, offset: int = 0) -> list[KnowledgeChunk]:
        stmt = sa.select(KnowledgeChunk).order_by(KnowledgeChunk.created_at.desc(), KnowledgeChunk.chunk_index)
        if source_type:
            stmt = stmt.where(KnowledgeChunk.source_type == source_type)
        return list(self.s.scalars(stmt.limit(limit).offset(offset)).all())

    def stats(self) -> dict:
        rows = self.s.execute(
            sa.select(KnowledgeChunk.source_type, sa.func.count()).group_by(KnowledgeChunk.source_type)
        ).all()
        by_type = {t: int(n) for t, n in rows}
        return {"total": sum(by_type.values()), "by_type": by_type}
