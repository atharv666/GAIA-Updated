"""Conversation sessions: memory, mode routing and persistence for Pipeline A."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..config import Settings
from ..core.activity import ACTIVITY
from ..db.base import utcnow
from ..db.models import ChatMessage, ChatSession
from ..providers.llm import ChatTurn
from .farm_assistant import FarmAssistant
from .open_assistant import OpenAssistant

Mode = Literal["farm", "open"]


class ChatService:
    def __init__(self, farm: FarmAssistant, open_: OpenAssistant, settings: Settings):
        self.farm = farm
        self.open = open_
        self.settings = settings

    def _history(self, s: Session, session_id: str) -> list[ChatTurn]:
        limit = self.settings.chat_history_turns * 2
        rows = s.scalars(
            sa.select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.desc())
            .limit(limit)
        ).all()
        return [ChatTurn("user" if m.role == "user" else "assistant", m.content) for m in reversed(rows)]

    def chat(self, s: Session, message: str, mode: Mode, session_id: str | None) -> dict:
        for kind, data in self.chat_events(s, message, mode, session_id, stream=False):
            if kind == "final":
                return data
        raise AssertionError("chat_events finished without a final event")

    def chat_events(
        self, s: Session, message: str, mode: Mode, session_id: str | None, *, stream: bool = True
    ) -> Iterator[tuple[str, object]]:
        """Answer and persist, yielding ("session", ...), ("stage", name), ("delta", text), ("final", dict)."""
        chat_session = s.get(ChatSession, session_id) if session_id else None
        if chat_session is None:
            chat_session = ChatSession(title=message[:80])
            s.add(chat_session)
        s.commit()  # end the transaction: ingestion writes from another session during answering
        history = self._history(s, chat_session.id)
        s.commit()
        yield ("session", chat_session.id)
        ACTIVITY.publish("chat_started", "Question asked: " + message[:90], mode=mode)

        events = (
            self.farm.answer_events(s, message, history, stream=stream)
            if mode == "farm"
            else self.open.answer_events(message, history, stream=stream)
        )
        result = None
        for kind, data in events:
            if kind == "final":
                result = data
            else:
                if kind == "stage":
                    ACTIVITY.publish("chat_stage", f"Chat: {data}", mode=mode, stage=data)
                yield (kind, data)
        assert result is not None
        payload = result.to_dict()

        s.add(ChatMessage(session_id=chat_session.id, role="user", mode=mode, content=message))
        assistant = ChatMessage(
            session_id=chat_session.id,
            role="assistant",
            mode=mode,
            content=result.answer,
            meta={k: payload[k] for k in ("sources", "grounding", "plan", "follow_ups", "llm_used", "notices")},
        )
        s.add(assistant)
        chat_session.updated_at = utcnow()
        s.commit()
        ACTIVITY.publish(
            "chat_finished", f"Answer ready with {len(result.sources)} cited source(s)",
            mode=mode, sources=len(result.sources), llm_used=result.llm_used,
            kinds=sorted({x.kind for x in result.sources}),
        )
        ACTIVITY.count("chats")
        yield ("final", {"session_id": chat_session.id, "message_id": assistant.id, **payload})

    def get_session(self, s: Session, session_id: str) -> dict | None:
        chat_session = s.get(ChatSession, session_id)
        if chat_session is None:
            return None
        return {
            "id": chat_session.id,
            "title": chat_session.title,
            "created_at": chat_session.created_at.isoformat(),
            "messages": [
                {
                    "id": m.id,
                    "role": m.role,
                    "mode": m.mode,
                    "content": m.content,
                    "created_at": m.created_at.isoformat(),
                    **({k: m.meta.get(k) for k in ("sources", "grounding", "follow_ups")} if m.meta else {}),
                }
                for m in chat_session.messages
            ],
        }

    def list_sessions(self, s: Session, limit: int = 20) -> list[dict]:
        rows = s.scalars(sa.select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)).all()
        return [{"id": r.id, "title": r.title, "updated_at": r.updated_at.isoformat()} for r in rows]
