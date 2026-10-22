"""Pipeline A chat API."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..container import Container
from .deps import get_container, get_db, require_api_key

log = logging.getLogger(__name__)

router = APIRouter(tags=["chat"], dependencies=[Depends(require_api_key)])


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    mode: Literal["farm", "open"] = "farm"
    session_id: str | None = Field(default=None, max_length=32)


@router.post("/chat")
def chat(req: ChatRequest, s: Session = Depends(get_db), c: Container = Depends(get_container)):
    """Ask GAIA. mode=farm -> "Query my farm" RAG; mode=open -> web-grounded open-ended answer."""
    return c.chat.chat(s, req.message.strip(), req.mode, req.session_id)


def _sse(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.post("/chat/stream")
def chat_stream(req: ChatRequest, c: Container = Depends(get_container)):
    """Same as ``POST /chat`` but streamed as Server-Sent Events.

    Events: ``session`` {session_id} · ``stage`` {name} · ``delta`` {text} (answer text as it is written) ·
    ``final`` (the complete ``/chat`` response; replaces the streamed draft, with citations validated) ·
    ``error`` {message}.
    """
    message = req.message.strip()

    def events() -> Iterator[str]:
        # The session is opened inside the generator: it must outlive the endpoint function.
        with c.session_factory() as s:
            try:
                for kind, data in c.chat.chat_events(s, message, req.mode, req.session_id, stream=True):
                    if kind == "session":
                        yield _sse("session", {"session_id": data})
                    elif kind == "stage":
                        yield _sse("stage", {"name": data})
                    elif kind == "delta":
                        yield _sse("delta", {"text": data})
                    else:
                        yield _sse("final", data)
            except Exception as exc:  # surface provider failures to the app instead of cutting the stream
                log.exception("Streaming chat failed")
                s.rollback()
                yield _sse("error", {"message": f"The AI service could not answer right now ({type(exc).__name__}). "
                                                "Please try again in a moment."})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.get("/chat/sessions")
def sessions(s: Session = Depends(get_db), c: Container = Depends(get_container)):
    return c.chat.list_sessions(s)


@router.get("/chat/sessions/{session_id}")
def session(session_id: str, s: Session = Depends(get_db), c: Container = Depends(get_container)):
    data = c.chat.get_session(s, session_id)
    if data is None:
        raise HTTPException(404, "Session not found")
    return data
