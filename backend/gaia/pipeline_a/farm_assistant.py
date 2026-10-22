"""Pipeline A, mode 1: "Query my farm" - sensor- and knowledge-grounded RAG.

    question
      -> ingestion sync (fresh windows, incl. the one still filling)
      -> query planning (LLM -> intent, sensors, time tokens; heuristic fallback)
      -> structured retrieval (exact SQL statistics  => [F#] facts)
      -> semantic retrieval  (pgvector + full-text, RRF => [K#] chunks)
      -> optional multi-hop  (conditions around a retrieved diagnosis)
      -> grounded generation (structured JSON, inline citations)
      -> citation validation (drop IDs that are not in the context)
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..config import Settings
from ..core.sensors import GAS_SENSORS
from ..core.timeutil import TimeRange, fmt_local, get_tz, resolve_time_expr
from ..db.base import utcnow
from ..db.models import SensorReading
from ..ingestion.service import IngestionService
from ..knowledge.store import (
    DIAGNOSIS_SOURCE_TYPES,
    FARMER_SOURCE_TYPES,
    SENSOR_SOURCE_TYPES,
    KnowledgeStore,
    ScoredChunk,
)
from ..providers.embeddings import Embedder
from ..providers.llm import LLM, ChatTurn, LLMError, LLMUnavailable
from .planner import SENSOR_INTENTS, QueryPlan, QueryPlanner
from .schemas import ChatAnswer, GroundedAnswer, SourceRef, reconcile_citations
from .sensor_facts import Fact, build_sensor_facts
from .streaming import generate_grounded

log = logging.getLogger(__name__)

MAX_CHUNK_CHARS = 1400

SOURCE_TYPE_LABELS = {
    "sensor_window": "Sensor window summary",
    "sensor_daily": "Daily sensor summary",
    "sensor_sessions": "Rover sensor on/off log",
    "sensor_digest_llm": "AI-generated daily interpretation",
    "diagnosis": "Crop diagnosis (AI visual assessment)",
    "agent_finding": "Agent-retrieved agricultural information",
    "farm_note": "Farmer note",
    "document": "Farm document",
}

FARM_SYSTEM = """You are GAIA, an agricultural assistant for {farmer} ({farm}). Mode: "Query my farm" - \
answer using the farmer's own data.

Grounding rules:
1. Farm-specific statements (measurements, times, diagnoses, treatments found earlier) must come from \
the CONTEXT and carry a citation such as [F1] or [K2] right after the statement. Never invent, estimate \
or extrapolate a farm measurement.
2. [F#] items are exact statistics computed from the sensor log. Quote their numbers as given; do not \
recompute them.
3. [K#] items come from the shared knowledge store: sensor summaries, earlier crop diagnoses (AI visual \
assessments - always call them possible or suspected, never confirmed), agent-retrieved agricultural \
information with web sources, and farmer notes. Items labelled "AI-generated interpretation" are \
interpretations, not measurements.
4. If the context does not contain what is needed, say so plainly (for example "I don't have soil \
moisture readings for yesterday"), mention what data IS available, and set insufficient_data=true.
5. You may add brief general agronomic advice, but only in a final paragraph that starts with \
"General guidance:" and then set used_general_knowledge=true.
6. MQ gas values are uncalibrated relative indices (raw ADC), never ppm. Always mention every \
"UNUSUAL READING" fact, even when the averages look normal.
7. Sensors can be switched on and off. When the context says a sensor was switched off, had no \
readings, or the rover was not connected, say so plainly and quote those periods exactly. Never estimate \
or imply values for them, and say which period an average actually covers. If a "Tip" is given, pass it on \
briefly. If advice depends on a sensor that was off, explain that the data is missing, then give clearly \
labelled general guidance.
8. Lead with the direct answer, then the supporting details. Be concise and practical; short paragraphs \
or bullet points; plain language a farmer understands. Current farm time: {now}."""


def _source_types_for(plan: QueryPlan, have_stats: bool) -> tuple[str, ...] | None:
    if plan.intent == "diagnosis_history":
        return DIAGNOSIS_SOURCE_TYPES
    if plan.intent in ("current_status", "sensor_statistics", "comparison", "trend"):
        if have_stats:
            # Exact facts already cover the numbers; per-window chunks would only repeat them.
            return ("sensor_daily", "sensor_sessions", "sensor_digest_llm") + FARMER_SOURCE_TYPES
        return SENSOR_SOURCE_TYPES + FARMER_SOURCE_TYPES
    return None  # farm_knowledge / general_advice: everything


def fact_source(f: Fact) -> SourceRef:
    return SourceRef(
        id=f.id, kind="sensor_fact", title=f.title, snippet=f.text, source_type=f"sensor_{f.kind}",
        time_start=f.time_start, time_end=f.time_end, meta=f.data,
    )


def chunk_source(cid: str, c: ScoredChunk) -> SourceRef:
    urls = [s.get("url") for s in (c.meta or {}).get("sources", []) if isinstance(s, dict)]
    return SourceRef(
        id=cid, kind="knowledge", title=c.title, snippet=c.content[:600], source_type=c.source_type,
        url=urls[0] if urls else None, time_start=c.time_start, time_end=c.time_end, score=round(c.score, 5),
        meta={k: v for k, v in (c.meta or {}).items() if k != "stats"},
    )


class FarmAssistant:
    def __init__(
        self,
        llm: LLM,
        embedder: Embedder,
        ingestion: IngestionService,
        settings: Settings,
    ):
        self.llm = llm
        self.embedder = embedder
        self.ingestion = ingestion
        self.settings = settings
        self.tz = get_tz(settings.farm_timezone)
        self.planner = QueryPlanner(llm, settings.farm_timezone)

    def answer(self, s: Session, question: str, history: list[ChatTurn], now: datetime | None = None) -> ChatAnswer:
        for kind, data in self.answer_events(s, question, history, now, stream=False):
            if kind == "final":
                return data
        raise AssertionError("answer_events finished without a final event")

    def answer_events(
        self, s: Session, question: str, history: list[ChatTurn], now: datetime | None = None, *, stream: bool = True
    ) -> Iterator[tuple[str, object]]:
        """Run the pipeline, yielding ("stage", name), ("delta", text) and finally ("final", ChatAnswer)."""
        timings: dict[str, int] = {}
        notices: list[str] = []
        t0 = time.perf_counter()

        def lap(name: str) -> None:
            nonlocal t0
            t1 = time.perf_counter()
            timings[name] = int((t1 - t0) * 1000)
            t0 = t1

        # 1. Ingestion phase: bring aggregates and the index up to date.
        yield ("stage", "syncing")
        sync = self.ingestion.sync(include_open=True, now=now)
        if sync.errors:
            notices.append("Some new sensor windows could not be indexed yet; statistics are still current.")
        now = now or utcnow()
        lap("ingest")

        # 2. Query understanding.
        yield ("stage", "planning")
        plan, planner_used = self.planner.plan(question, history, now)
        ranges: list[TimeRange] = [r for r in (resolve_time_expr(t, now, self.tz) for t in plan.time_ranges) if r]
        plan.sensors = self._installed_sensors(s, plan.sensors)
        lap("plan")

        # 3a. Structured retrieval over the sensor log.
        yield ("stage", "retrieving")
        facts: list[Fact] = []
        if plan.intent in SENSOR_INTENTS or (plan.sensors and ranges and plan.intent != "small_talk"):
            facts = build_sensor_facts(
                s, intent=plan.intent, sensors=plan.sensors, ranges=ranges, tz=self.tz,
                window_minutes=self.settings.window_minutes, now=now,
            )

        # 3b. Semantic + lexical retrieval over the shared knowledge store.
        chunks: list[ScoredChunk] = []
        if plan.search_knowledge:
            store = KnowledgeStore(s, self.embedder)
            # Time-filter sensor-type questions; diagnoses/notes are searched across all time.
            t_start = t_end = None
            if ranges and plan.intent != "diagnosis_history":
                t_start, t_end = min(r.start for r in ranges), max(r.end for r in ranges)
            have_stats = any(f.kind in ("stats", "latest") for f in facts)
            k = min(self.settings.rag_top_k, 3) if have_stats and plan.intent in SENSOR_INTENTS else self.settings.rag_top_k
            try:
                chunks = store.search(
                    plan.search_query, k=k, source_types=_source_types_for(plan, have_stats),
                    time_start=t_start, time_end=t_end,
                )
            except LLMError as exc:
                notices.append("Knowledge-store search is temporarily unavailable (embedding provider error).")
                log.warning("Knowledge search failed: %s", exc)

        # 3c. Multi-hop: sensor conditions around retrieved diagnoses.
        if plan.link_conditions:
            from .sensor_facts import FactBuilder

            fb = FactBuilder(s, self.tz, self.settings.window_minutes)
            fb.facts = list(facts)
            for c in [c for c in chunks if c.source_type == "diagnosis" and c.time_start][:2]:
                label = f"the {c.meta.get('plant', 'crop')} diagnosis ({c.meta.get('diagnosis', 'finding')})"
                fb.linked_conditions(label, c.time_start, plan.sensors or None)
            facts = fb.facts
        lap("retrieve")

        # 4. Context assembly.
        fact_refs = [fact_source(f) for f in facts]
        chunk_refs = [chunk_source(f"K{i}", c) for i, c in enumerate(chunks, start=1)]
        context = fact_refs + chunk_refs
        plan_dict = {
            **plan.model_dump(),
            "planner": planner_used,
            "resolved_ranges": [r.to_dict(self.tz) for r in ranges],
            "ingestion": sync.to_dict(),
        }

        if plan.intent == "small_talk" and not context:
            final = self._small_talk(question, history, plan_dict, timings, notices)
            yield ("delta", final.answer)
            yield ("final", final)
            return

        # 5. Grounded generation.
        yield ("stage", "generating")
        context_text = self._render_context(facts, chunks)
        try:
            out = yield from generate_grounded(
                self.llm,
                system=FARM_SYSTEM.format(
                    farmer=self.settings.farmer_name, farm=self.settings.farm_name,
                    now=fmt_local(now, self.tz, "%A %d %B %Y %H:%M"),
                ),
                turns=[*history, ChatTurn("user", f"CONTEXT\n{context_text}\n\nQUESTION\n{question}")],
                temperature=0.2,
                stream=stream,
            )
            llm_used = True
        except LLMUnavailable as exc:
            out, llm_used = self._offline_answer(facts, chunks), False
            notices.append(str(exc))
        lap("generate")

        # 6. Citation validation.
        valid = {r.id for r in context}
        answer, cited = reconcile_citations(out.answer, out.cited, valid)
        by_id = {r.id: r for r in context}
        sources = [by_id[c] for c in cited]
        grounding = {
            "farm_data": any(c.startswith("F") for c in cited),
            "knowledge_store": any(c.startswith("K") for c in cited),
            "general_knowledge": out.used_general_knowledge,
            "web": False,
            "insufficient_data": out.insufficient_data,
        }
        yield ("final", ChatAnswer(
            mode="farm", answer=answer, sources=sources, context=context, grounding=grounding,
            plan=plan_dict, follow_ups=out.follow_ups[:3], llm_used=llm_used, notices=notices,
            timings_ms=timings,
        ))

    @staticmethod
    def _installed_sensors(s: Session, sensors: list[str]) -> list[str]:
        """Drop gas sensors that have never reported when "gas" was expanded to all MQ modules.

        A rover fitted with only MQ5 + MQ6 should not make every gas question answer "no MQ2 data".
        """
        gas = [k for k in sensors if k in GAS_SENSORS]
        if len(gas) < 2:
            return sensors
        reporting = set(s.scalars(sa.select(SensorReading.sensor).where(SensorReading.sensor.in_(gas)).distinct()))
        if not reporting:
            return sensors
        return [k for k in sensors if k not in GAS_SENSORS or k in reporting]

    def _render_context(self, facts: list[Fact], chunks: list[ScoredChunk]) -> str:
        parts = []
        if facts:
            parts.append("FARM DATA - exact statistics computed from the sensor log:")
            parts += [f"[{f.id}] {f.text}" for f in facts]
        else:
            parts.append("FARM DATA - no sensor statistics were needed or available for this question.")
        parts.append("")
        if chunks:
            parts.append("KNOWLEDGE STORE - retrieved records:")
            for i, c in enumerate(chunks, start=1):
                when = f" · {fmt_local(c.time_start, self.tz, '%a %d %b %Y %H:%M')}" if c.time_start else ""
                label = SOURCE_TYPE_LABELS.get(c.source_type, c.source_type)
                body = c.content if len(c.content) <= MAX_CHUNK_CHARS else c.content[:MAX_CHUNK_CHARS] + " …"
                parts.append(f"[K{i}] ({label}{when}) {c.title}\n{body}")
        else:
            parts.append("KNOWLEDGE STORE - no relevant records found.")
        return "\n".join(parts)

    @staticmethod
    def _offline_answer(facts: list[Fact], chunks: list[ScoredChunk]) -> GroundedAnswer:
        lines = ["**AI model not configured — showing the retrieved farm data directly.**", ""]
        lines += [f"- {f.text} [{f.id}]" for f in facts]
        lines += [f"- {c.title}: {c.content[:280]}… [K{i}]" for i, c in enumerate(chunks[:3], start=1)]
        if not facts and not chunks:
            lines.append("No matching farm data was found.")
        return GroundedAnswer(
            answer="\n".join(lines),
            cited=[f.id for f in facts] + [f"K{i}" for i in range(1, min(len(chunks), 3) + 1)],
            insufficient_data=not facts and not chunks,
        )

    def _small_talk(self, question, history, plan_dict, timings, notices) -> ChatAnswer:
        text = (
            f"Hello {self.settings.farmer_name}! Ask me about your field — for example "
            "“What was the average soil moisture today?” or “What crop problems have been found?”"
        )
        llm_used = False
        if self.llm.available:
            try:
                text = self.llm.complete(
                    system=(
                        f"You are GAIA, a friendly farm assistant for {self.settings.farmer_name}. Reply briefly "
                        "(1-2 sentences). Offer to help with their sensor data or crop diagnoses."
                    ),
                    turns=[*history[-4:], ChatTurn("user", question)],
                    temperature=0.5,
                    max_tokens=300,
                ).strip()
                llm_used = True
            except LLMError:
                pass
        return ChatAnswer(
            mode="farm", answer=text, sources=[], context=[], plan=plan_dict, llm_used=llm_used,
            grounding={"farm_data": False, "knowledge_store": False, "general_knowledge": False, "web": False,
                       "insufficient_data": False},
            notices=notices, timings_ms=timings,
        )
