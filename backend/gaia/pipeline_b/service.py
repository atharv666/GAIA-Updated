"""Pipeline B orchestration: image -> VLM -> single agent search -> present + knowledge write.

Independence guarantees (see context.md, constraints 2 and 6):
* this module never reads sensor tables;
* it only WRITES to the shared knowledge store; it never reads chat history;
* it is never triggered by Pipeline A.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..core.chunking import chunk_text
from ..core.activity import ACTIVITY
from ..core.timeutil import fmt_local, get_tz
from ..db.base import utcnow
from ..db.models import Diagnosis
from ..knowledge.store import ChunkDraft, KnowledgeStore
from ..providers.embeddings import Embedder
from ..providers.llm import LLM, LLMError, LLMUnavailable
from ..providers.websearch import WebSearch
from .agent import AgentFindings, AgentRun, SearchAgent
from .vlm import CONFIDENCE_NOTE, DISCLAIMER, VisionAnalyzer, VLMDiagnosis, prepare_image, recapture_tips

log = logging.getLogger(__name__)

VALID_SOURCES = ("rover", "phone", "web", "api")


class UploadTooLarge(ValueError):
    pass


class DiagnosisService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        vlm: LLM,
        agent_llm: LLM,
        search: WebSearch,
        embedder: Embedder,
        settings: Settings,
    ):
        self.session_factory = session_factory
        self.vlm = vlm
        self.agent_llm = agent_llm
        self.search = search
        self.embedder = embedder
        self.settings = settings
        self.tz = get_tz(settings.farm_timezone)

    # -------------------------------------------------------------- lifecycle

    def create(self, s: Session, data: bytes, source: str, note: str | None) -> Diagnosis:
        if len(data) > self.settings.max_upload_mb * 1024 * 1024:
            raise UploadTooLarge(f"Image larger than {self.settings.max_upload_mb} MB")
        prepared = prepare_image(data, self.settings.vlm_max_image_px)
        now = utcnow()
        rel = Path(f"{now:%Y}") / f"{now:%m}" / f"{prepared.sha256[:16]}-{uuid4().hex[:8]}.jpg"
        path = self.settings.uploads_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(prepared.jpeg)
        d = Diagnosis(
            source=source if source in VALID_SOURCES else "api",
            note=(note or "").strip()[:500] or None,
            image_path=rel.as_posix(),
            image_sha256=prepared.sha256,
            status="processing",
            stage="queued",
            timings={"image": {"width": prepared.width, "height": prepared.height,
                               "original_bytes": prepared.original_bytes, "format": prepared.original_format}},
        )
        s.add(d)
        s.commit()
        return d

    def image_path(self, d: Diagnosis) -> Path:
        return self.settings.uploads_dir / d.image_path

    def run(self, diagnosis_id: str) -> None:
        """Execute the pipeline for a queued diagnosis (called in a worker thread)."""
        with self.session_factory() as s:
            d = s.get(Diagnosis, diagnosis_id)
            if d is None:
                return
            timings = dict(d.timings or {})
            try:
                jpeg = self.image_path(d).read_bytes()

                # Step 1 - VLM analysis.
                self._stage(s, d, "vlm")
                t = time.perf_counter()
                analysis = VisionAnalyzer(self.vlm).analyze(jpeg)
                timings["vlm_ms"] = int((time.perf_counter() - t) * 1000)
                d.analysis, d.vlm_model = analysis.model_dump(), self.vlm.model_id
                if not analysis.is_plant_image:
                    d.status, d.stage, d.timings = "not_plant", "done", timings
                    s.commit()
                    return

                # Step 2 - single agent search ("uncertain" follows the unhealthy branch).
                self._stage(s, d, "searching")
                t = time.perf_counter()
                agent = SearchAgent(
                    self.agent_llm, self.search, region=self.settings.farm_region,
                    max_rounds=self.settings.agent_max_rounds,
                    queries_per_round=self.settings.agent_queries_per_round,
                )
                run = agent.run(analysis)
                timings["agent_ms"] = int((time.perf_counter() - t) * 1000)
                d.findings = run.findings.model_dump()
                d.sources = run.sources()
                d.agent_trace = run.trace
                d.web_grounded = run.web_grounded
                d.agent_model = self.agent_llm.model_id

                # Step 3 - present + augment the shared knowledge store (one-way write).
                self._stage(s, d, "writing")
                t = time.perf_counter()
                d.knowledge_chunk_ids = self._write_knowledge(s, d, analysis, run)
                timings["knowledge_ms"] = int((time.perf_counter() - t) * 1000)
                d.status, d.stage, d.timings = "completed", "done", timings
                s.commit()
                ACTIVITY.publish(
                    "diagnosis_finished", f"Diagnosis done: {analysis.plant}, {analysis.health_status}",
                    diagnosis_id=d.id, status="completed", plant=analysis.plant, health=analysis.health_status,
                )
                ACTIVITY.count("diagnoses")
            except Exception as exc:  # the job must always end in a terminal state
                s.rollback()
                d = s.get(Diagnosis, diagnosis_id)
                d.status, d.stage, d.timings = "failed", "done", timings
                ACTIVITY.publish("diagnosis_finished", "Diagnosis failed", diagnosis_id=diagnosis_id, status="failed")
                d.error = str(exc) if isinstance(exc, LLMError) else f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, LLMUnavailable):
                    log.exception("Diagnosis %s failed", diagnosis_id)
                s.commit()

    def _stage(self, s: Session, d: Diagnosis, stage: str) -> None:
        d.stage = stage
        s.commit()
        ACTIVITY.publish("diagnosis_stage", f"Diagnosis: {stage}", diagnosis_id=d.id, stage=stage)

    def recover_interrupted(self) -> int:
        """Mark jobs that were running when the server stopped as failed."""
        with self.session_factory() as s:
            result = s.execute(
                sa.update(Diagnosis)
                .where(Diagnosis.status == "processing")
                .values(status="failed", stage="done", error="Interrupted by a server restart; please retry.")
            )
            s.commit()
            return result.rowcount or 0

    # -------------------------------------------------------------- knowledge

    def _write_knowledge(self, s: Session, d: Diagnosis, a: VLMDiagnosis, run: AgentRun) -> list[str]:
        store = KnowledgeStore(s, self.embedder)
        when = fmt_local(d.created_at, self.tz, "%a %d %b %Y %H:%M")
        base_meta = {
            "diagnosis_id": d.id,
            "plant": a.plant,
            "health_status": a.health_status,
            "diagnosis": a.diagnosis,
            "confidence": a.confidence,
            "capture_source": d.source,
            "note": d.note,
            "image_url": f"/api/v1/diagnoses/{d.id}/image",
        }
        ids = store.upsert_source("diagnosis", d.id, [self._diagnosis_draft(d, a, when, base_meta)])
        ids += store.upsert_source("agent_finding", d.id, self._findings_drafts(d, a, run, when, base_meta))
        return ids

    def _diagnosis_draft(self, d: Diagnosis, a: VLMDiagnosis, when: str, meta: dict) -> ChunkDraft:
        status_txt = {
            "healthy": "healthy",
            "unhealthy": "unhealthy (possible issue, not confirmed)",
            "uncertain": "uncertain (the visual assessment could not reach a clear conclusion)",
        }[a.health_status]
        part = f" {a.plant_part}" if a.plant_part else ""
        lines = [
            f"On {when} (farm time) GAIA's visual AI assessed a photo of a {a.plant}{part} "
            f"({d.source} capture{f', farmer note: {d.note}' if d.note else ''}).",
            f"Health status: {status_txt}. Assessment: {a.diagnosis}.",
        ]
        if a.issue_categories:
            lines.append("Issue categories: " + ", ".join(c.replace("_", " ") for c in a.issue_categories) + ".")
        if a.evidence:
            lines.append("Visual evidence: " + "; ".join(a.evidence) + ".")
        if a.differential:
            lines.append("Other possibilities considered: " + "; ".join(a.differential) + ".")
        lines.append(
            f"Model-reported confidence {a.confidence:.0%} (rough indicator, not a calibrated probability); "
            f"image quality {a.image_quality}; visible severity {a.severity}."
        )
        return ChunkDraft(
            title=f"Crop diagnosis · {a.plant} · {a.health_status} · {when}",
            content="\n".join(lines),
            time_start=d.created_at,
            time_end=d.created_at,
            meta={**meta, "generated_by": "vlm", "model": self.vlm.model_id},
        )

    def _findings_drafts(self, d: Diagnosis, a: VLMDiagnosis, run: AgentRun, when: str, meta: dict) -> list[ChunkDraft]:
        f: AgentFindings = run.findings
        domain_of = {f"W{i}": r.domain for i, r in enumerate(run.results, start=1)}

        def cite(text: str) -> str:
            # [W2] markers only mean something inside this diagnosis; store the domain instead.
            return re.sub(r"\[(W\d+)\]", lambda m: f"({domain_of.get(m.group(1), 'web')})", text)

        sections = [
            ("Summary", [f.headline]),
            ("About the suspected condition", [f.condition_overview] if f.condition_overview else []),
            ("Symptoms to check", f.symptoms_to_check),
            ("Possible causes", f.possible_causes),
            ("Recommended actions", f.recommended_actions),
            ("Prevention", f.preventive_measures),
            ("Care guidance", f.care_guidance),
            ("How to confirm", f.distinguishing_checks),
            ("When to consult an expert", [f.when_to_consult_expert] if f.when_to_consult_expert else []),
        ]
        body = [f"Agricultural information found by GAIA's search agent on {when} for {a.plant} "
                f"({a.diagnosis}; status {a.health_status})."]
        for heading, items in sections:
            items = [cite(i) for i in items if i]
            if len(items) == 1:
                body.append(f"{heading}: {items[0]}")
            elif items:
                body.append(f"{heading}:\n" + "\n".join(f"- {i}" for i in items))
        cited = [r for i, r in enumerate(run.results, start=1) if f"W{i}" in run.cited]
        if cited:
            body.append("Sources: " + "; ".join(f"{r.title} ({r.url})" for r in cited))
        else:
            body.append("No web sources were cited; this information is based on the model's general knowledge.")
        text = "\n".join(body)
        pieces = chunk_text(text, size=1500, overlap=150)
        title = f"Agricultural information · {a.plant} · {a.diagnosis}"
        source_meta = [{"title": r.title, "url": r.url, "domain": r.domain, "quality": r.quality} for r in cited]
        return [
            ChunkDraft(
                title=title if len(pieces) == 1 else f"{title} (part {i}/{len(pieces)})",
                content=piece,
                time_start=d.created_at,
                time_end=d.created_at,
                meta={**meta, "generated_by": "agent", "model": self.agent_llm.model_id,
                      "web_grounded": run.web_grounded, "sources": source_meta},
            )
            for i, piece in enumerate(pieces, start=1)
        ]

    # ---------------------------------------------------------- presentation

    def to_dict(self, d: Diagnosis) -> dict:
        analysis = VLMDiagnosis.model_validate(d.analysis) if d.analysis else None
        return {
            "id": d.id,
            "created_at": d.created_at.isoformat(),
            "updated_at": d.updated_at.isoformat() if d.updated_at else None,
            "source": d.source,
            "note": d.note,
            "status": d.status,
            "stage": d.stage,
            "error": d.error,
            "image_url": f"/api/v1/diagnoses/{d.id}/image",
            "analysis": d.analysis,
            "confidence_note": CONFIDENCE_NOTE,
            "disclaimer": DISCLAIMER,
            "recapture_tips": recapture_tips(analysis) if analysis else [],
            "findings": d.findings,
            "sources": d.sources or [],
            "web_grounded": d.web_grounded,
            "agent_trace": d.agent_trace or [],
            "knowledge_chunk_ids": d.knowledge_chunk_ids or [],
            "models": {"vlm": d.vlm_model, "agent": d.agent_model},
            "timings": d.timings or {},
        }

    def summary(self, d: Diagnosis) -> dict:
        a = d.analysis or {}
        return {
            "id": d.id,
            "created_at": d.created_at.isoformat(),
            "source": d.source,
            "note": d.note,
            "status": d.status,
            "stage": d.stage,
            "plant": a.get("plant"),
            "health_status": a.get("health_status"),
            "diagnosis": a.get("diagnosis"),
            "confidence": a.get("confidence"),
            "image_url": f"/api/v1/diagnoses/{d.id}/image",
        }

    def list(self, s: Session, limit: int = 20, offset: int = 0) -> list[Diagnosis]:
        return list(
            s.scalars(sa.select(Diagnosis).order_by(Diagnosis.created_at.desc()).limit(limit).offset(offset)).all()
        )

    def latest_completed_at(self, s: Session) -> datetime | None:
        return s.scalar(sa.select(sa.func.max(Diagnosis.created_at)).where(Diagnosis.status == "completed"))
