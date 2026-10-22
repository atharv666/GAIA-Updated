"""Token streaming for grounded answers.

The answer generator returns one JSON object (``GroundedAnswer``). Streaming the raw JSON to a phone
would show braces and keys, so ``AnswerFieldStreamer`` incrementally decodes only the value of the
``answer`` property while the model is still writing it. The complete object is validated at the end
(with a non-streaming repair attempt if the stream was malformed), and the caller replaces the
streamed draft with the citation-validated final text.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Generator, Sequence

from pydantic import ValidationError

from ..providers.llm import LLM, ChatTurn, extract_json
from .schemas import GroundedAnswer

log = logging.getLogger(__name__)

_ANSWER_START = re.compile(r'"answer"\s*:\s*"')
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


class AnswerFieldStreamer:
    """Feed raw JSON chunks; get back the newly available text of the ``"answer"`` string."""

    def __init__(self) -> None:
        self._buf = ""
        self._pos = 0
        self._in_answer = False
        self.done = False

    def feed(self, chunk: str) -> str:
        if self.done:
            return ""
        self._buf += chunk
        if not self._in_answer:
            m = _ANSWER_START.search(self._buf)
            if not m:
                return ""
            self._in_answer = True
            self._pos = m.end()
        out: list[str] = []
        buf, i = self._buf, self._pos
        while i < len(buf):
            ch = buf[i]
            if ch == '"':
                self.done = True
                i += 1
                break
            if ch != "\\":
                out.append(ch)
                i += 1
                continue
            if i + 1 >= len(buf):
                break  # escape sequence split across chunks
            esc = buf[i + 1]
            if esc in _SIMPLE_ESCAPES:
                out.append(_SIMPLE_ESCAPES[esc])
                i += 2
            elif esc == "u":
                if i + 6 > len(buf):
                    break
                try:
                    cp = int(buf[i + 2 : i + 6], 16)
                except ValueError:
                    out.append("?")
                    i += 2
                    continue
                if 0xD800 <= cp < 0xDC00:  # high surrogate: wait for the low half
                    if i + 12 > len(buf):
                        break
                    try:
                        low = int(buf[i + 8 : i + 12], 16)
                        cp = 0x10000 + ((cp - 0xD800) << 10) + (low - 0xDC00)
                    except ValueError:
                        cp = 0xFFFD
                    i += 12
                else:
                    i += 6
                out.append(chr(cp))
            else:
                out.append(esc)
                i += 2
        self._pos = i
        return "".join(out)


def generate_grounded(
    llm: LLM,
    *,
    system: str,
    turns: Sequence[ChatTurn],
    temperature: float,
    stream: bool,
) -> Generator[tuple[str, str], None, GroundedAnswer]:
    """Produce a ``GroundedAnswer``; with ``stream=True`` also yield ``("delta", text)`` events."""
    if not stream:
        return llm.structured(system=system, turns=turns, schema=GroundedAnswer, temperature=temperature)

    streamer = AnswerFieldStreamer()
    raw: list[str] = []
    for chunk in llm.stream_structured(system=system, turns=turns, schema=GroundedAnswer, temperature=temperature):
        raw.append(chunk)
        text = streamer.feed(chunk)
        if text:
            yield ("delta", text)
    full = _THINK_RE.sub("", "".join(raw))
    try:
        return GroundedAnswer.model_validate_json(extract_json(full))
    except ValidationError as exc:
        log.info("Streamed answer was not valid JSON (%s); regenerating without streaming", exc.errors()[:2])
        return llm.structured(system=system, turns=turns, schema=GroundedAnswer, temperature=temperature)
