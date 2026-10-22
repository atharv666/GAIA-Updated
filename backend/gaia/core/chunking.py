"""Recursive text splitter for farmer notes and uploaded documents."""

from __future__ import annotations

import re

_SEPARATORS = ("\n\n", "\n", ". ", "; ", ", ", " ")


def _split(text: str, size: int, seps: tuple[str, ...]) -> list[str]:
    if len(text) <= size:
        return [text]
    if not seps:
        return [text[i : i + size] for i in range(0, len(text), size)]
    sep, rest = seps[0], seps[1:]
    parts = text.split(sep)
    if len(parts) == 1:
        return _split(text, size, rest)
    pieces: list[str] = []
    for i, part in enumerate(parts):
        piece = part + (sep if i < len(parts) - 1 else "")
        pieces.extend(_split(piece, size, rest) if len(piece) > size else [piece])
    return pieces


def chunk_text(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
    """Split ``text`` into ~``size`` character chunks on natural boundaries with overlap."""
    text = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n")).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if not text:
        return []
    pieces = _split(text, size, _SEPARATORS)
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > size:
            chunks.append(current.strip())
            tail = current[-overlap:] if overlap else ""
            # Start the overlap on a word boundary.
            if " " in tail:
                tail = tail[tail.index(" ") + 1 :]
            current = tail + piece
        else:
            current += piece
    if current.strip():
        chunks.append(current.strip())
    return chunks
