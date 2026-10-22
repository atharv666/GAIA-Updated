"""Pipeline B, step 2: ONE research agent with a web-search tool.

The agent receives only the VLM's structured JSON (no sensor data, no image),
decides what to look up based on the health branch, runs a bounded
plan -> search -> observe loop (it chooses queries and when to stop), and then
writes cited findings. This is a single agent step, not a multi-agent cascade.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from ..core.citations import split_grouped_citations
from ..providers.llm import LLM, ChatTurn
from ..providers.websearch import QUALITY_RANK, WebResult, WebSearch
from .vlm import VLMDiagnosis

log = logging.getLogger(__name__)

MAX_SOURCES = 12


class AgentStep(BaseModel):
    rationale: str = Field(description="One or two sentences: what information is still missing and why")
    queries: list[str] = Field(default_factory=list, description="New web search queries to run now (0-3)")
    done: bool = Field(description="True when the collected results are enough to write the findings")


class AgentFindings(BaseModel):
    headline: str = Field(description="One-sentence summary for the farmer")
    condition_overview: str = Field(default="", description="What the suspected condition is; empty if healthy")
    symptoms_to_check: list[str] = Field(default_factory=list, description="Symptoms the farmer can look for")
    possible_causes: list[str] = Field(default_factory=list, description="Causes and conditions that favour it")
    recommended_actions: list[str] = Field(default_factory=list, description="Practical next steps, most important first")
    preventive_measures: list[str] = Field(default_factory=list, description="How to prevent it or its spread")
    care_guidance: list[str] = Field(default_factory=list, description="Cultivation and care guidance (healthy plants)")
    distinguishing_checks: list[str] = Field(
        default_factory=list, description="How to tell the suspected condition apart from look-alikes"
    )
    when_to_consult_expert: str = Field(default="", description="When to contact an agronomist or extension officer")
    cited_sources: list[str] = Field(default_factory=list, description="IDs like W1 of every source relied upon")


PLANNER_SYSTEM = """You are the single research agent in GAIA's crop-diagnosis pipeline. You receive the \
structured output of a vision-language model (VLM) that examined a crop photo, and you decide what \
agricultural information to look up on the web so the farmer can act on it.

Branch policy:
- healthy: cultivation and care for this crop - irrigation, nutrition / fertiliser, growth conditions, \
common pests or diseases to watch for, harvest timing.
- unhealthy: the suspected condition - symptoms, causes and conditions favouring spread, treatment \
options (cultural, biological, chemical), preventive measures.
- uncertain: treat like unhealthy, AND look up how to distinguish the suspected condition from the \
look-alikes in the VLM's differential, so the farmer can confirm it. Keep the uncertainty in mind.

Search tips: include the crop name and the condition; prefer university, extension and government \
sources; add the region ({region}) for treatments, products and seasons. Do not repeat queries that were \
already run. Set done=true when the results cover the branch policy or more searching will not help."""

WRITER_SYSTEM = """You write the findings of GAIA's crop-diagnosis research agent for a farmer.
Inputs: the VLM visual assessment and the web search results [W#].
Rules:
- Base factual statements on the web results and cite them inline, e.g. "Remove infected lower \
leaves [W2]." Use only the [W#] IDs provided, no other markers such as [VLM]. Put every ID you rely \
on in cited_sources.
- If there are no web results, use general agronomic knowledge and say so in the headline.
- Preserve uncertainty: the diagnosis is a POSSIBLE condition from an AI visual assessment, not \
confirmed. If the status is uncertain, fill distinguishing_checks and say the diagnosis needs confirmation.
- healthy: fill care_guidance (irrigation, nutrition, growth conditions, what to watch for); leave the \
treatment fields empty.
- unhealthy / uncertain: fill condition_overview, symptoms_to_check, possible_causes, \
recommended_actions (cultural and biological measures first; chemical options only as "consult the \
product label / local extension"), preventive_measures.
- Never give exact pesticide doses. Each list item is one short practical sentence in plain language.
- when_to_consult_expert: when to contact an agronomist, KVK or extension officer."""


BRANCH = {
    "healthy": "healthy - look up cultivation and care",
    "unhealthy": "unhealthy - look up the suspected condition, treatment and prevention",
    "uncertain": "uncertain - treat like unhealthy and look up distinguishing symptoms; keep the uncertainty",
}


@dataclass
class AgentRun:
    findings: AgentFindings
    results: list[WebResult]
    cited: list[str]
    trace: list[dict] = field(default_factory=list)
    web_grounded: bool = False

    def sources(self) -> list[dict]:
        return [
            {**r.to_dict(), "id": f"W{i}", "cited": f"W{i}" in self.cited}
            for i, r in enumerate(self.results, start=1)
        ]


def _fallback_queries(a: VLMDiagnosis, region: str) -> list[str]:
    plant = a.plant if a.plant.lower() != "unknown" else "crop"
    if a.health_status == "healthy":
        return [f"{plant} cultivation care irrigation fertilizer guide", f"{plant} common pests diseases prevention"]
    condition = re.sub(r"^(possible|suspected|likely|probable)\s+", "", a.diagnosis, flags=re.I)
    queries = [f"{plant} {condition} symptoms management", f"{plant} {condition} treatment {region}"]
    if a.health_status == "uncertain" and a.differential:
        queries.append(f"{plant} {condition} vs {a.differential[0]} how to distinguish")
    return queries


_W_RE = re.compile(r"\[(W\d{1,2})\]")
# Invented pseudo-citations such as [VLM] or [Source]; the only real markers are [W#].
_FAKE_CITE_RE = re.compile(r"\s*\[(?!W\d)[A-Z][A-Za-z]{1,9}\]")
_PLACEHOLDERS = {"na", "none", "notapplicable", "nil", "notavailable", "nothing", "", "nodata"}


class SearchAgent:
    def __init__(self, llm: LLM, search: WebSearch, *, region: str, max_rounds: int, queries_per_round: int,
                 results_per_query: int = 4):
        self.llm = llm
        self.search = search
        self.region = region
        self.max_rounds = max(1, max_rounds)
        self.queries_per_round = max(1, queries_per_round)
        self.results_per_query = results_per_query

    def run(self, analysis: VLMDiagnosis) -> AgentRun:
        vlm_json = json.dumps(analysis.model_dump(), indent=1, ensure_ascii=False)
        results: list[WebResult] = []
        seen_urls: set[str] = set()
        run_queries: list[str] = []
        trace: list[dict] = []

        if self.search.available:
            for round_no in range(1, self.max_rounds + 1):
                state = self._render_state(vlm_json, analysis, round_no, run_queries, results)
                step = self.llm.structured(
                    system=PLANNER_SYSTEM.format(region=self.region),
                    turns=[ChatTurn("user", state)],
                    schema=AgentStep,
                    temperature=0.2,
                )
                queries = [q.strip() for q in step.queries if q.strip() and q.strip() not in run_queries]
                if round_no == 1 and not queries:
                    queries = _fallback_queries(analysis, self.region)
                queries = queries[: self.queries_per_round]
                trace.append({"round": round_no, "rationale": step.rationale, "queries": queries,
                              "done": step.done and round_no > 1})
                if (step.done and round_no > 1) or not queries:
                    break
                run_queries += queries
                new = 0
                for r in self.search.search_many(queries, self.results_per_query):
                    key = r.url.split("#")[0].rstrip("/")
                    if key not in seen_urls and len(results) < MAX_SOURCES:
                        seen_urls.add(key)
                        results.append(r)
                        new += 1
                trace[-1]["new_results"] = new
                if step.done:
                    break
        else:
            trace.append({"round": 0, "rationale": "Web search is not configured; writing from general knowledge.",
                          "queries": []})

        # Rounds are ranked individually; re-rank the whole set so the best sources come first.
        results.sort(key=lambda r: QUALITY_RANK[r.quality])
        findings = self._write(vlm_json, analysis, results)
        valid = {f"W{i}" for i in range(1, len(results) + 1)}
        findings, cited = _clean_citations(findings, valid)
        return AgentRun(findings=findings, results=results, cited=cited, trace=trace, web_grounded=bool(cited))

    def _render_state(self, vlm_json, analysis, round_no, run_queries, results) -> str:
        lines = [
            f"VLM RESULT:\n{vlm_json}",
            f"BRANCH: {BRANCH[analysis.health_status]}",
            f"ROUND: {round_no} of {self.max_rounds}",
            "QUERIES ALREADY RUN:\n" + ("\n".join(f"- {q}" for q in run_queries) or "- none"),
            "RESULTS SO FAR:",
        ]
        lines += [f"[W{i}] {r.title} ({r.domain}, {r.quality})\n{r.snippet[:280]}" for i, r in enumerate(results, 1)]
        if not results:
            lines.append("none")
        lines.append("Decide the next step.")
        return "\n".join(lines)

    def _write(self, vlm_json: str, analysis: VLMDiagnosis, results: list[WebResult]) -> AgentFindings:
        sources = "\n\n".join(
            f"[W{i}] {r.title} ({r.domain}, {r.quality} source)\n{r.snippet}" for i, r in enumerate(results, 1)
        ) or "none"
        return self.llm.structured(
            system=WRITER_SYSTEM,
            turns=[ChatTurn("user", f"VLM RESULT:\n{vlm_json}\n\nBRANCH: {BRANCH[analysis.health_status]}\n\n"
                                    f"WEB RESULTS:\n{sources}\n\nWrite the findings.")],
            schema=AgentFindings,
            temperature=0.2,
        )


def _clean_citations(f: AgentFindings, valid: set[str]) -> tuple[AgentFindings, list[str]]:
    cited: list[str] = []

    def fix(text: str) -> str:
        def keep(m: re.Match) -> str:
            if m.group(1) in valid:
                if m.group(1) not in cited:
                    cited.append(m.group(1))
                return m.group(0)
            return ""
        text = _FAKE_CITE_RE.sub("", _W_RE.sub(keep, split_grouped_citations(text)))
        return re.sub(r"\s+([.,;])", r"\1", text).strip()

    def placeholder(text: str) -> bool:
        # Models sometimes fill sections that do not apply with "N/A" rows instead of leaving them empty.
        return re.sub(r"[^a-z]", "", text.lower()) in _PLACEHOLDERS

    data = f.model_dump()
    for key, value in data.items():
        if key == "cited_sources":
            continue
        if isinstance(value, str):
            value = fix(value)
            data[key] = "" if placeholder(value) else value
        elif isinstance(value, list):
            items = (fix(v) for v in value if isinstance(v, str))
            data[key] = [v for v in items if v and not placeholder(v)]
    for cid in f.cited_sources:
        cid = cid.strip("[] ")
        if cid in valid and cid not in cited:
            cited.append(cid)
    data["cited_sources"] = cited
    return AgentFindings.model_validate(data), cited
