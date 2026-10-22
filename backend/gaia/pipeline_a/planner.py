"""Query understanding for "Query my farm".

The LLM classifies intent, picks sensors and emits time-expression *tokens*;
``core.timeutil`` resolves them to exact ranges. A rule-based planner provides
the same output when no LLM is configured or the LLM call fails.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..core.sensors import GAS_SENSORS, SENSORS, rag_sensor_keys
from ..core.timeutil import TIME_EXPR_HELP, get_tz, resolve_time_expr
from ..providers.llm import LLM, ChatTurn, LLMError

log = logging.getLogger(__name__)

Intent = Literal[
    "current_status",
    "sensor_statistics",
    "comparison",
    "trend",
    "diagnosis_history",
    "farm_knowledge",
    "general_advice",
    "small_talk",
]

SENSOR_INTENTS = {"current_status", "sensor_statistics", "comparison", "trend", "general_advice"}


class QueryPlan(BaseModel):
    intent: Intent = Field(description="Primary intent of the farmer's message")
    sensors: list[str] = Field(default_factory=list, description="Sensor registry keys relevant to the question")
    time_ranges: list[str] = Field(
        default_factory=list, description="Time-expression tokens, most recent first for comparisons"
    )
    search_knowledge: bool = Field(default=True, description="Search the shared knowledge store")
    link_conditions: bool = Field(
        default=False, description="Farmer asks about sensor conditions at the time a diagnosed problem was found"
    )
    search_query: str = Field(description="Standalone rewrite of the question for semantic search")


def _sensor_lines() -> str:
    return "\n".join(f"- {s.key}: {s.label} [{s.unit}] ({s.hardware})" for s in SENSORS.values() if s.rag)


PLANNER_SYSTEM = """You are the query-understanding step of GAIA, a farm assistant. Convert the \
farmer's latest message into a retrieval plan over their farm database. Do NOT answer the question.

Current farm-local time: {now} ({tz}).

Sensors (key: meaning):
{sensors}

Time expressions - use ONLY these forms:
{time_help}

Intents:
- current_status: ONLY how things are right now / the latest reading (a named period such as \
"this morning" or "yesterday" is sensor_statistics)
- sensor_statistics: averages, minimums, maximums or values over a period, or a field summary
- comparison: compare periods (e.g. today vs yesterday); list EVERY period, most recent first
- trend: how something changed over a period
- diagnosis_history: crop problems, diagnoses, pests or treatments previously found by GAIA's image diagnosis
- farm_knowledge: farmer notes / documents or other stored farm information
- general_advice: advice that should use the farm's own data (e.g. "should I water today?")
- small_talk: greetings, thanks, chit-chat

Rules:
- sensors: registry keys only. "gas" or "air quality" means mq2, mq5 and mq6. "moisture" alone means \
soil_moisture. For general_advice choose the sensors that matter (watering -> soil_moisture, humidity, \
temperature). For a field summary list all sensors.
- time_ranges: if no period is mentioned use "now" for current_status and "day:0" otherwise. \
"this morning" -> daypart:0:morning, "last night" -> daypart:-1:night, "past week" -> last_days:7. \
A comparison like "lower than yesterday" means ["day:0", "day:-1"].
- search_knowledge: false only for purely numeric sensor questions or small talk.
- link_conditions: true only when the farmer asks about sensor conditions around the time a crop \
problem was detected.
- search_query: a standalone rewrite of the question that resolves pronouns and follow-ups using the \
conversation, for semantic search."""


class QueryPlanner:
    def __init__(self, llm: LLM, timezone: str):
        self.llm = llm
        self.tz = get_tz(timezone)

    def plan(self, question: str, history: list[ChatTurn], now: datetime) -> tuple[QueryPlan, str]:
        """Returns the plan and which planner produced it ('llm' or 'heuristic')."""
        if self.llm.available:
            try:
                convo = "\n".join(f"{t.role}: {t.content[:400]}" for t in history[-6:])
                prompt = (
                    (f"Conversation so far:\n{convo}\n\n" if convo else "")
                    + f"Farmer's latest message: {question}"
                )
                system = PLANNER_SYSTEM.format(
                    now=now.astimezone(self.tz).strftime("%A %d %B %Y, %H:%M"),
                    tz=self.tz.key,
                    sensors=_sensor_lines(),
                    time_help=TIME_EXPR_HELP,
                )
                plan = self.llm.structured(
                    system=system, turns=[ChatTurn("user", prompt)], schema=QueryPlan, temperature=0.0
                )
                return self._sanitize(plan, question, now), "llm"
            except LLMError as exc:
                log.warning("LLM planner failed, using heuristic planner: %s", exc)
        return self._sanitize(heuristic_plan(question, history), question, now), "heuristic"

    def _sanitize(self, plan: QueryPlan, question: str, now: datetime) -> QueryPlan:
        sensors = []
        for s in plan.sensors:
            key = s.strip().lower()
            if key in ("gas", "air_quality"):
                sensors.extend(GAS_SENSORS)
            elif key in SENSORS and SENSORS[key].rag and key not in sensors:
                sensors.append(key)
        ranges = [r for r in dict.fromkeys(t.strip().lower() for t in plan.time_ranges)
                  if resolve_time_expr(r, now, self.tz)]
        if plan.intent in SENSOR_INTENTS:
            sensors = sensors or rag_sensor_keys()
            ranges = ranges or (["now"] if plan.intent == "current_status" else ["day:0"])
        if plan.intent == "comparison" and len(ranges) == 1:
            ranges = _implied_comparison(ranges[0])
        # Deterministic backstops for common planner slips (especially with small models):
        intent = plan.intent
        if intent == "current_status" and any(r != "now" for r in ranges):
            intent = "sensor_statistics"  # a named past period is never "right now"
        search = plan.search_knowledge and intent != "small_talk"
        if intent in ("diagnosis_history", "farm_knowledge"):
            search = True  # these questions can only be answered from the knowledge store
        link = plan.link_conditions or (
            intent == "diagnosis_history" and bool(re.search(_WORDS["conditions"], question.lower()))
        )
        return plan.model_copy(
            update={
                "intent": intent,
                "sensors": list(dict.fromkeys(sensors))[:6],
                "time_ranges": ranges[:4],
                "search_query": plan.search_query.strip() or question,
                "search_knowledge": search,
                "link_conditions": link,
            }
        )


def _implied_comparison(expr: str) -> list[str]:
    if m := re.fullmatch(r"day:(-?\d+)", expr):
        n = int(m.group(1))
        return [f"day:{n + 1}", expr] if n < 0 else ["day:0", "day:-1"]
    if m := re.fullmatch(r"daypart:(-?\d+):(\w+)", expr):
        n, part = int(m.group(1)), m.group(2)
        return [f"daypart:{n + 1}:{part}", expr] if n < 0 else [expr, f"daypart:-1:{part}"]
    if m := re.fullmatch(r"week:(-?\d+)", expr):
        n = int(m.group(1))
        return [f"week:{n + 1}", expr] if n < 0 else ["week:0", "week:-1"]
    return ["day:0", expr] if expr != "day:0" else ["day:0", "day:-1"]


# --------------------------------------------------------------------------
# Rule-based fallback planner
# --------------------------------------------------------------------------

_TIME_RULES: list[tuple[str, object]] = [
    (r"\bday before yesterday\b", "day:-2"),
    (r"\byesterday (early morning)\b", "daypart:-1:early_morning"),
    (r"\byesterday (morning|afternoon|evening)\b", lambda m: f"daypart:-1:{m.group(1)}"),
    (r"\blast night\b", "daypart:-1:night"),
    (r"\bthis (morning|afternoon|evening)\b", lambda m: f"daypart:0:{m.group(1)}"),
    (r"\btonight\b", "daypart:0:night"),
    (r"\byesterday\b", "day:-1"),
    (r"\btoday\b", "day:0"),
    (r"\b(?:past|last) (\d{1,3}) ?(?:hours?|hrs?)\b", lambda m: f"last_hours:{m.group(1)}"),
    (r"\b(?:past|last) (\d{1,2}) days?\b", lambda m: f"last_days:{m.group(1)}"),
    (r"\b(?:past|last) (?:24 hours|day)\b", "last_hours:24"),
    (r"\bpast week\b|\blast 7 days\b", "last_days:7"),
    (r"\bthis week\b", "week:0"),
    (r"\blast week\b", "week:-1"),
    (r"\b(?:right now|currently|at the moment|latest|current|now)\b", "now"),
]

_WORDS = {
    "small_talk": r"^\s*(hi|hello|hey|hii+|namaste|thanks|thank you|good (morning|evening|night)|ok(ay)?)\b[\s!.?]*$",
    "diagnosis": r"diagnos|disease|pest|blight|infect|fung|mildew|rot\b|wilt|treat|remed|problem|issue|sick|"
                 r"deficien|symptom|spray|leaf spot|yellowing|identified|detected",
    "conditions": r"condition|weather|environment|humid|temperature|when (it|this|that) was",
    "comparison": r"compar|\bthan\b|\bvs\b|versus|differen|higher|lower|more than|less than|increase|decrease",
    "trend": r"trend|over time|chang|pattern|through the day|rising|falling|drop|fluctuat",
    "advice": r"should i|when should|do i need|recommend|advice|suggest|is it (a )?good time|ok to",
    "summary": r"summar|overview|report|how is my (farm|field)|how are my|status of (my )?(farm|field)",
    "notes": r"\bnote|document|planted|sow|fertili[sz]|harvest(ed)? on|record(ed)? that",
}


def heuristic_plan(question: str, history: list[ChatTurn]) -> QueryPlan:
    q = question.lower().strip()
    if re.search(_WORDS["small_talk"], q):
        return QueryPlan(intent="small_talk", search_knowledge=False, search_query=question)

    # Short follow-ups ("what about yesterday?") inherit the previous user question.
    context_q = q
    if len(q.split()) <= 5 and history:
        prev_user = next((t.content.lower() for t in reversed(history) if t.role == "user"), "")
        context_q = f"{prev_user} {q}"

    ranges: list[str] = []
    scratch = q
    for pattern, token in _TIME_RULES:
        for m in list(re.finditer(pattern, scratch)):
            ranges.append(token(m) if callable(token) else token)
        scratch = re.sub(pattern, " ", scratch)

    sensors: list[str] = []
    if re.search(r"\bgas|air quality|smell|leak", context_q):
        sensors.extend(GAS_SENSORS)
    for spec in SENSORS.values():
        if spec.rag and any(k in context_q for k in spec.keywords) and spec.key not in sensors:
            sensors.append(spec.key)
    if "humid" in context_q and "soil" not in context_q and "moisture" not in context_q.replace("air moisture", ""):
        sensors = [s for s in sensors if s != "soil_moisture"]

    diag = bool(re.search(_WORDS["diagnosis"], q))
    link = diag and bool(re.search(_WORDS["conditions"], q))
    if diag:
        intent = "diagnosis_history"
    elif (re.search(_WORDS["notes"], q) or re.match(r"\s*where\b", q)
          or (re.match(r"\s*(which|who)\b", q) and not sensors)):
        sensors, ranges = [], []  # a question about farm facts, not measurements
        intent = "farm_knowledge"
    elif re.search(_WORDS["comparison"], q) and (sensors or ranges):
        intent = "comparison"
    elif re.search(_WORDS["trend"], q):
        intent = "trend"
    elif re.search(_WORDS["advice"], q) or re.search(r"\bwater\b", q):
        intent = "general_advice"
        if not sensors or re.search(r"\bwater|irrigat", q):
            sensors += [k for k in ("soil_moisture", "humidity", "temperature") if k not in sensors]
        ranges = ranges or ["last_hours:24"]
    elif "now" in ranges:
        intent = "current_status"
    elif sensors or ranges or re.search(_WORDS["summary"], q):
        intent = "sensor_statistics"
    else:
        intent = "farm_knowledge"

    if intent == "diagnosis_history" and link and not sensors:
        sensors = rag_sensor_keys()
    return QueryPlan(
        intent=intent,
        sensors=sensors,
        time_ranges=ranges,
        search_knowledge=intent not in ("current_status",),
        link_conditions=link,
        search_query=question if context_q == q else context_q,
    )
