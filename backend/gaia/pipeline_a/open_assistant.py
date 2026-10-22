"""Pipeline A, mode 2: open-ended questions answered with web search grounding.

    question -> search planning (standalone rewrite + 0-3 queries)
             -> web search (parallel, de-duplicated, institutional sources first)
             -> grounded generation with [W#] citations -> citation validation
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator

from pydantic import BaseModel, Field

from ..config import Settings
from ..providers.llm import LLM, ChatTurn, LLMError, LLMUnavailable
from ..providers.websearch import WebResult, WebSearch
from .schemas import ChatAnswer, GroundedAnswer, SourceRef, reconcile_citations
from .streaming import generate_grounded

log = logging.getLogger(__name__)


class SearchPlan(BaseModel):
    standalone_question: str = Field(description="The question rewritten to stand alone without the conversation")
    needs_search: bool = Field(description="True if web search would materially improve the answer")
    queries: list[str] = Field(default_factory=list, description="1-3 concise web search queries")


SEARCH_PLANNER = """You plan web searches for GAIA, an assistant for farmers in {region}.
Given the conversation and the latest message:
- Rewrite the message as a standalone question.
- needs_search = true for factual, current, regional or technical questions (crop practices, pests, \
market prices, weather, government schemes, product information, news, anything you are unsure about). \
false for greetings, thanks, simple arithmetic or pure opinion.
- Write 1-3 short, specific search queries. Add the region when location matters (prices, schemes, \
varieties, seasons)."""

OPEN_SYSTEM = """You are GAIA, a helpful assistant for farmers (region: {region}). Mode: open-ended \
questions - general farming or any other topic.
- Use the WEB RESULTS when relevant and cite them inline as [W1], [W2]. Prefer institutional / \
extension sources when results disagree.
- If the results are missing or irrelevant, answer from general knowledge, say that it is general \
knowledge rather than a live source, and set used_general_knowledge=true.
- In this mode you cannot see the farmer's own sensor data or diagnoses; suggest switching to \
"Query my farm" for questions about their field.
- For pesticides or chemicals: recommend following the product label, local regulations and \
protective equipment, and consulting a local agricultural extension officer for dosage.
- Be concise and practical. Use short paragraphs or bullet points."""

_SMALL_TALK = re.compile(r"^\s*(hi|hello|hey|namaste|thanks|thank you|ok(ay)?|good (morning|evening|night))\b[\s!.?]*$", re.I)


def web_source(cid: str, r: WebResult) -> SourceRef:
    return SourceRef(
        id=cid, kind="web", title=r.title, snippet=r.snippet[:500], source_type="web", url=r.url,
        domain=r.domain, quality=r.quality, meta={"query": r.query},
    )


class OpenAssistant:
    def __init__(self, llm: LLM, search: WebSearch, settings: Settings):
        self.llm = llm
        self.search = search
        self.settings = settings

    def _plan(self, question: str, history: list[ChatTurn]) -> tuple[SearchPlan, str]:
        if self.llm.available:
            try:
                convo = "\n".join(f"{t.role}: {t.content[:400]}" for t in history[-6:])
                plan = self.llm.structured(
                    system=SEARCH_PLANNER.format(region=self.settings.farm_region),
                    turns=[ChatTurn("user", (f"Conversation:\n{convo}\n\n" if convo else "") + f"Latest message: {question}")],
                    schema=SearchPlan,
                    temperature=0.0,
                )
                plan.queries = [q for q in plan.queries if q.strip()][:3]
                if plan.needs_search and not plan.queries:
                    plan.queries = [plan.standalone_question or question]
                return plan, "llm"
            except LLMError as exc:
                log.warning("Search planner failed: %s", exc)
        small = bool(_SMALL_TALK.match(question))
        return SearchPlan(standalone_question=question, needs_search=not small, queries=[] if small else [question]), "heuristic"

    def answer(self, question: str, history: list[ChatTurn]) -> ChatAnswer:
        for kind, data in self.answer_events(question, history, stream=False):
            if kind == "final":
                return data
        raise AssertionError("answer_events finished without a final event")

    def answer_events(
        self, question: str, history: list[ChatTurn], *, stream: bool = True
    ) -> Iterator[tuple[str, object]]:
        """Yield ("stage", name), ("delta", text) and finally ("final", ChatAnswer)."""
        timings: dict[str, int] = {}
        notices: list[str] = []
        t0 = time.perf_counter()

        yield ("stage", "planning")
        plan, planner_used = self._plan(question, history)
        timings["plan"] = int((time.perf_counter() - t0) * 1000)

        t1 = time.perf_counter()
        results: list[WebResult] = []
        if plan.needs_search:
            yield ("stage", "searching")
            if self.search.available:
                results = self.search.search_many(plan.queries, self.settings.search_max_results)[:8]
                if not results:
                    notices.append("Web search returned no results; answering from general knowledge.")
            else:
                notices.append("Web search is not configured; answering from general knowledge.")
        timings["search"] = int((time.perf_counter() - t1) * 1000)

        context = [web_source(f"W{i}", r) for i, r in enumerate(results, start=1)]
        context_text = (
            "WEB RESULTS:\n" + "\n\n".join(
                f"[{c.id}] {c.title} ({c.domain}, {c.quality} source)\n{c.snippet}" for c in context
            )
            if context else "WEB RESULTS: none."
        )

        t2 = time.perf_counter()
        yield ("stage", "generating")
        try:
            out = yield from generate_grounded(
                self.llm,
                system=OPEN_SYSTEM.format(region=self.settings.farm_region),
                turns=[*history, ChatTurn("user", f"{context_text}\n\nQUESTION\n{question}")],
                temperature=0.3,
                stream=stream,
            )
            llm_used = True
        except LLMUnavailable as exc:
            llm_used = False
            notices.append(str(exc))
            if context:
                body = "\n".join(f"- [{c.title}]({c.url}) — {c.snippet[:200]}… [{c.id}]" for c in context[:5])
                out = GroundedAnswer(
                    answer=f"**AI model not configured — here are the top web results.**\n\n{body}",
                    cited=[c.id for c in context[:5]],
                )
            else:
                out = GroundedAnswer(answer="The AI model is not configured, so I can't answer open questions yet.",
                                     insufficient_data=True)
        timings["generate"] = int((time.perf_counter() - t2) * 1000)

        answer, cited = reconcile_citations(out.answer, out.cited, {c.id for c in context})
        by_id = {c.id: c for c in context}
        yield ("final", ChatAnswer(
            mode="open",
            answer=answer,
            sources=[by_id[c] for c in cited],
            context=context,
            grounding={
                "farm_data": False,
                "knowledge_store": False,
                "general_knowledge": out.used_general_knowledge or not cited,
                "web": bool(cited),
                "insufficient_data": out.insufficient_data,
            },
            plan={**plan.model_dump(), "planner": planner_used, "search_provider": self.search.name},
            follow_ups=out.follow_ups[:3],
            llm_used=llm_used,
            notices=notices,
            timings_ms=timings,
        ))
