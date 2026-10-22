"""Citation helpers shared by both pipelines (kept neutral so the pipelines stay independent)."""

from __future__ import annotations

import re

_GROUPED_CITE_RE = re.compile(r"\[([FKW]\d{1,3}(?:\s*[,;]\s*[FKW]\d{1,3})+)\]")


def split_grouped_citations(text: str) -> str:
    """"[W10, W12]" -> "[W10][W12]" so every citation can be validated and rendered."""
    return _GROUPED_CITE_RE.sub(lambda m: "".join(f"[{c}]" for c in re.split(r"\s*[,;]\s*", m.group(1))), text)
