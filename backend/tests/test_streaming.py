"""Token streaming: the JSON ``answer`` field decoder, the /chat/stream SSE endpoint and the providers' streams."""

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gaia.pipeline_a.schemas import GroundedAnswer
from gaia.pipeline_a.streaming import AnswerFieldStreamer
from gaia.providers.llm import ChatTurn, GeminiLLM, OpenAICompatLLM

from .conftest import FIXED_NOW, IST, ScriptedLLM, seed_series


class StreamingLLM(ScriptedLLM):
    """ScriptedLLM that streams its GroundedAnswer JSON in small pieces."""

    def stream_structured(self, *, system, turns, schema, images=(), temperature=0.2, max_tokens=4096) -> Iterator[str]:
        self.calls.append((f"stream:{schema.__name__}", system, list(turns)))
        raw = schema.model_validate(self.handlers[schema.__name__](system, turns)).model_dump_json()
        for i in range(0, len(raw), 7):
            yield raw[i : i + 7]


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


# --- incremental decoder -----------------------------------------------------------------------

ANSWER = 'Soil is "dry" — 35.2% [F1].\nSecond line with \\ backslash and emoji 🌱, tab\t.'
PAYLOAD = json.dumps({"answer": ANSWER, "cited": ["F1"], "follow_ups": ["Why?"]})


@pytest.mark.parametrize("size", [1, 2, 3, 5, 11, 1000])
def test_answer_streamer_decodes_any_chunking(size):
    for ensure_ascii in (True, False):
        payload = json.dumps(json.loads(PAYLOAD), ensure_ascii=ensure_ascii)
        streamer = AnswerFieldStreamer()
        out = "".join(streamer.feed(payload[i : i + size]) for i in range(0, len(payload), size))
        assert out == ANSWER
        assert streamer.done


def test_answer_streamer_ignores_fields_before_answer_and_after_it():
    streamer = AnswerFieldStreamer()
    text = '{"cited": ["F1"], "answer": "Hi", "used_general_knowledge": false}'
    assert streamer.feed(text) == "Hi" and streamer.done and streamer.feed("more") == ""


# --- /chat/stream ------------------------------------------------------------------------------


@pytest.fixture
def stream_llm() -> StreamingLLM:
    return StreamingLLM()


@pytest.fixture
def stream_client(settings, stream_llm, vlm, search):
    from fastapi.testclient import TestClient

    from gaia.container import Container
    from gaia.main import create_app
    from gaia.providers.embeddings import HashingEmbedder

    container = Container.build(
        settings, llm=stream_llm, vlm=vlm, embedder=HashingEmbedder(settings.embedding_dim), search=search
    )
    with TestClient(create_app(container=container)) as c:
        c.container = container
        yield c


def test_chat_stream_farm_mode_emits_stages_deltas_and_validated_final(stream_client, stream_llm):
    seed_series(stream_client.container, start_local=FIXED_NOW.astimezone(IST).replace(hour=6, minute=0), minutes=60,
                values=lambda i: {"soil_moisture": 40.0, "humidity": 70, "temperature": 25})
    stream_llm.handlers["QueryPlan"] = lambda s, t: {
        "intent": "current_status", "sensors": ["soil_moisture"], "time_ranges": ["last_hours:24"],
        "search_knowledge": False, "link_conditions": False, "search_query": "soil moisture",
    }
    stream_llm.handlers["GroundedAnswer"] = lambda s, t: {
        "answer": "Soil moisture is fine [F1]. A made-up claim [F99].", "cited": ["F1", "F99"],
    }
    r = stream_client.post("/api/v1/chat/stream", json={"message": "How is my soil?", "mode": "farm"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(r.text)
    kinds = [k for k, _ in events]
    assert kinds[0] == "session" and kinds[-1] == "final"
    assert [d["name"] for k, d in events if k == "stage"] == ["syncing", "planning", "retrieving", "generating"]
    streamed = "".join(d["text"] for k, d in events if k == "delta")
    assert streamed.startswith("Soil moisture is fine [F1].")
    assert sum(1 for k in kinds if k == "delta") > 3  # genuinely incremental, not one blob
    final = events[-1][1]
    assert "[F99]" not in final["answer"] and "[F1]" in final["answer"]  # hallucinated citation removed
    assert [s["id"] for s in final["sources"]] == ["F1"]
    assert final["session_id"] == events[0][1]["session_id"]

    # Persisted exactly like the non-streaming endpoint, so history and follow-ups keep working.
    session = stream_client.get(f"/api/v1/chat/sessions/{final['session_id']}").json()
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]
    assert session["messages"][1]["content"] == final["answer"]


def test_chat_stream_open_mode_reports_search_stage(stream_client, stream_llm, search):
    stream_llm.handlers["SearchPlan"] = lambda s, t: {
        "standalone_question": "How to treat early blight?", "needs_search": True, "queries": ["early blight tomato"],
    }
    stream_llm.handlers["GroundedAnswer"] = lambda s, t: {"answer": "Remove infected leaves [W1].", "cited": ["W1"]}
    events = _sse_events(stream_client.post(
        "/api/v1/chat/stream", json={"message": "How to treat early blight?", "mode": "open"}
    ).text)
    assert [d["name"] for k, d in events if k == "stage"] == ["planning", "searching", "generating"]
    final = events[-1][1]
    assert final["grounding"]["web"] and final["sources"][0]["id"] == "W1"
    assert search.queries == ["early blight tomato"]


def test_chat_stream_reports_provider_failure_as_error_event(stream_client, stream_llm):
    # No handlers registered: the planner falls back to heuristics, but answer generation raises.
    r = stream_client.post("/api/v1/chat/stream", json={"message": "What is the humidity?", "mode": "farm"})
    events = _sse_events(r.text)
    assert events[-1][0] == "error" and "try again" in events[-1][1]["message"]


def test_chat_stream_requires_api_key_when_configured(stream_client):
    stream_client.container.settings.api_key = "secret"
    try:
        assert stream_client.post("/api/v1/chat/stream", json={"message": "hi"}).status_code == 401
    finally:
        stream_client.container.settings.api_key = None


def test_non_streaming_chat_still_uses_structured_call(client, llm):
    llm.handlers["GroundedAnswer"] = lambda s, t: {"answer": "Hello there.", "cited": []}
    llm.handlers["QueryPlan"] = lambda s, t: {"intent": "small_talk", "sensors": [], "time_ranges": [],
                                              "search_knowledge": False, "link_conditions": False,
                                              "search_query": "hello"}
    r = client.post("/api/v1/chat", json={"message": "hello", "mode": "farm"})
    assert r.status_code == 200 and "session_id" in r.json()
    assert not any(name.startswith("stream:") for name in llm.schemas_called())


# --- providers' real wire streams (local fake servers) ------------------------------------------


class _SSEServer:
    def __init__(self, frames: list[str], path_check: str):
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body))
                assert path_check in self.path
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for frame in frames:
                    self.wfile.write(frame.encode())
                    self.wfile.flush()

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def _pieces(text: str, n: int = 9) -> list[str]:
    return [text[i : i + n] for i in range(0, len(text), n)]


def test_openai_compat_stream_parses_sse_deltas():
    full = json.dumps({"answer": "Humidity is 62% [F1].", "cited": ["F1"]})
    frames = [f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n\n" for p in _pieces(full)]
    frames.insert(0, f"data: {json.dumps({'choices': [{'delta': {'role': 'assistant'}}]})}\n\n")
    frames.append("data: [DONE]\n\n")
    api = _SSEServer(frames, "/chat/completions")
    try:
        llm = OpenAICompatLLM("test-model", api.url, "key")
        chunks = list(llm.stream_structured(system="sys", turns=[ChatTurn("user", "q")], schema=GroundedAnswer))
    finally:
        api.close()
    assert "".join(chunks) == full and len(chunks) > 2
    assert api.requests[0][1]["stream"] is True
    assert GroundedAnswer.model_validate_json("".join(chunks)).cited == ["F1"]


def test_gemini_stream_parses_sse_deltas():
    full = json.dumps({"answer": "Soil averaged 40.5% [F1].", "cited": ["F1"]})
    frames = [
        "data: " + json.dumps({"candidates": [{"content": {"role": "model", "parts": [{"text": p}]}}]}) + "\r\n\r\n"
        for p in _pieces(full)
    ]
    api = _SSEServer(frames, ":streamGenerateContent")
    try:
        from google import genai
        from google.genai import types

        llm = GeminiLLM("gemini-2.5-flash", "test-key")
        llm._client = genai.Client(api_key="test-key", http_options=types.HttpOptions(base_url=api.url + "/"))
        chunks = list(llm.stream_structured(system="sys", turns=[ChatTurn("user", "q")], schema=GroundedAnswer))
    finally:
        api.close()
    assert "".join(chunks) == full and len(chunks) > 2
    assert api.requests[0][1]["generationConfig"]["responseMimeType"] == "application/json"
