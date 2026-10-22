"""Structured types shared by both chat modes."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from ..core.citations import split_grouped_citations


class GroundedAnswer(BaseModel):
    """What the answer-generation LLM must return."""

    answer: str = Field(
        description="Markdown answer for the farmer with inline citations such as [F1], [K2] or [W1]"
    )
    cited: list[str] = Field(default_factory=list, description="IDs of every context item the answer relies on")
    used_general_knowledge: bool = Field(
        default=False, description="True if any part of the answer relies on general knowledge, not the context"
    )
    insufficient_data: bool = Field(
        default=False, description="True if the context lacked data needed to answer fully"
    )
    follow_ups: list[str] = Field(
        default_factory=list, description="Up to 3 short follow-up questions the farmer might ask next"
    )


@dataclass
class SourceRef:
    id: str
    kind: Literal["sensor_fact", "knowledge", "web"]
    title: str
    snippet: str
    source_type: str | None = None
    url: str | None = None
    domain: str | None = None
    quality: str | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    score: float | None = None
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "snippet": self.snippet,
            "source_type": self.source_type,
            "url": self.url,
            "domain": self.domain,
            "quality": self.quality,
            "time_start": self.time_start.isoformat() if self.time_start else None,
            "time_end": self.time_end.isoformat() if self.time_end else None,
            "score": self.score,
            "meta": self.meta,
        }


@dataclass
class ChatAnswer:
    mode: Literal["farm", "open"]
    answer: str
    sources: list[SourceRef]
    context: list[SourceRef]
    grounding: dict
    plan: dict
    follow_ups: list[str] = field(default_factory=list)
    llm_used: bool = True
    notices: list[str] = field(default_factory=list)
    timings_ms: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "answer": self.answer,
            "sources": [s.to_dict() for s in self.sources],
            "context": [s.to_dict() for s in self.context],
            "grounding": self.grounding,
            "plan": self.plan,
            "follow_ups": self.follow_ups,
            "llm_used": self.llm_used,
            "notices": self.notices,
            "timings_ms": self.timings_ms,
        }


_CITE_RE = re.compile(r"\[([FKW]\d{1,3})\]")


def reconcile_citations(answer: str, reported: list[str], valid_ids: set[str]) -> tuple[str, list[str]]:
    """Drop citations to context items that do not exist (hallucinated IDs).

    Returns the cleaned answer and the ordered list of valid cited IDs.
    """
    answer = split_grouped_citations(answer)
    cited: list[str] = []

    def keep(m: re.Match) -> str:
        cid = m.group(1)
        if cid in valid_ids:
            if cid not in cited:
                cited.append(cid)
            return m.group(0)
        return ""

    cleaned = _CITE_RE.sub(keep, answer)
    cleaned = re.sub(r"[ \t]+([.,;:])", r"\1", cleaned)
    for cid in reported:
        cid = cid.strip("[] ")
        if cid in valid_ids and cid not in cited:
            cited.append(cid)
    return cleaned.strip(), cited
