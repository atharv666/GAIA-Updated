"""Contract tests: run the real provider code against local fake APIs (no network, no keys).

They verify what the Gemini SDK / OpenAI-compatible client actually send (structured-output schema,
system instruction, inline images, embedding dimensionality) and that responses are parsed back into
the pipeline schemas.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from gaia.pipeline_a.planner import QueryPlan
from gaia.pipeline_a.schemas import GroundedAnswer
from gaia.pipeline_b.agent import AgentFindings, AgentStep
from gaia.pipeline_b.vlm import VLMDiagnosis
from gaia.providers.embeddings import GeminiEmbedder, OpenAICompatEmbedder
from gaia.providers.llm import ChatTurn, GeminiLLM, ImagePart, OpenAICompatLLM

from .conftest import jpeg_bytes

VLM_JSON = {
    "is_plant_image": True, "plant": "Tomato", "health_status": "uncertain", "diagnosis": "Possible early blight",
    "issue_categories": ["fungal_infection"], "evidence": ["Brown concentric lesions"], "confidence": 0.55,
    "image_quality": "fair",
}


class FakeAPI:
    """Minimal HTTP server; `reply(path, body) -> dict` decides the JSON response."""

    def __init__(self, reply):
        self.requests: list[tuple[str, dict]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append((self.path, body, dict(self.headers)))
                payload = json.dumps(reply(self.path, body)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()


def gemini_reply(path, body):
    if path.endswith(":batchEmbedContents") or path.endswith(":embedContent"):
        n = len(body.get("requests", [body]))
        return {"embeddings": [{"values": [3.0, 4.0] + [0.0] * 766} for _ in range(n)]}
    schema = json.dumps(body.get("generationConfig", {}).get("responseSchema", {})).lower()
    if "health_status" in schema:
        text = json.dumps(VLM_JSON)
    elif "done" in schema:
        text = json.dumps({"rationale": "need treatment info", "queries": ["tomato early blight"], "done": False})
    else:
        text = json.dumps({"answer": "Soil averaged 40.5% [F1].", "cited": ["F1"]})
    return {"candidates": [{"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}]}


@pytest.fixture
def gemini_api():
    api = FakeAPI(gemini_reply)
    yield api
    api.close()


def _point_at(obj, url):
    from google import genai
    from google.genai import types

    obj._client = genai.Client(api_key="test-key", http_options=types.HttpOptions(base_url=url + "/"))


def test_gemini_vlm_sends_schema_image_and_system_prompt(gemini_api):
    llm = GeminiLLM("gemini-2.5-flash", "test-key")
    _point_at(llm, gemini_api.url)
    result = llm.structured(
        system="You are the visual-analysis step.",
        turns=[ChatTurn("user", "Analyse this crop photograph.")],
        images=[ImagePart(jpeg_bytes(), "image/jpeg")],
        schema=VLMDiagnosis,
    )
    assert isinstance(result, VLMDiagnosis) and result.health_status == "uncertain"
    path, body, headers = gemini_api.requests[-1]
    assert path.endswith("models/gemini-2.5-flash:generateContent")
    assert headers.get("x-goog-api-key") == "test-key"
    config = body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    props = config["responseSchema"]["properties"]
    assert {"plant", "health_status", "diagnosis", "evidence", "confidence"} <= set(props)
    assert set(props["health_status"]["enum"]) == {"healthy", "unhealthy", "uncertain"}
    assert body["systemInstruction"]["parts"][0]["text"].startswith("You are the visual-analysis step")
    parts = body["contents"][-1]["parts"]
    inline = parts[0]["inlineData"]
    assert (inline.get("mimeType") or inline.get("mime_type")) == "image/jpeg" and inline["data"]
    assert parts[-1]["text"] == "Analyse this crop photograph."


@pytest.mark.parametrize("schema", [QueryPlan, GroundedAnswer, AgentStep, AgentFindings])
def test_gemini_accepts_every_pipeline_schema(gemini_api, schema):
    llm = GeminiLLM("gemini-2.5-flash", "test-key")
    _point_at(llm, gemini_api.url)
    try:
        llm.structured(system="s", turns=[ChatTurn("user", "q")], schema=schema)
    except Exception:
        pass  # canned replies do not match every schema; we only inspect the request
    body = gemini_api.requests[0][1]
    assert body["generationConfig"]["responseSchema"]["type"].upper() == "OBJECT"


def test_gemini_multi_turn_roles_and_embeddings(gemini_api):
    llm = GeminiLLM("gemini-2.5-flash", "test-key")
    _point_at(llm, gemini_api.url)
    out = llm.structured(
        system="s",
        turns=[ChatTurn("user", "hi"), ChatTurn("assistant", "hello"), ChatTurn("user", "soil?")],
        schema=GroundedAnswer,
    )
    assert out.cited == ["F1"]
    roles = [c["role"] for c in gemini_api.requests[-1][1]["contents"]]
    assert roles == ["user", "model", "user"]

    emb = GeminiEmbedder("gemini-embedding-001", 768, "test-key")
    _point_at(emb, gemini_api.url)
    vectors = emb.embed_documents(["a", "b"])
    assert len(vectors) == 2 and vectors[0][:2] == pytest.approx([0.6, 0.8])  # L2-normalised
    path, body, _ = gemini_api.requests[-1]
    assert path.endswith(":batchEmbedContents")
    assert body["requests"][0]["outputDimensionality"] == 768
    assert body["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"


def openai_reply(path, body):
    if path.endswith("/embeddings"):
        return {"data": [{"index": i, "embedding": [0.0, 2.0] + [0.0] * 766} for i in range(len(body["input"]))]}
    return {"choices": [{"message": {"role": "assistant", "content": "```json\n" + json.dumps(VLM_JSON) + "\n```"}}]}


def test_openai_compatible_vision_json_mode_and_embeddings():
    api = FakeAPI(openai_reply)
    try:
        llm = OpenAICompatLLM("meta-llama/llama-4-scout-17b-16e-instruct", api.url + "/openai/v1", "groq-key")
        result = llm.structured(
            system="Analyse.", turns=[ChatTurn("user", "photo")], images=[ImagePart(b"\xff\xd8jpeg", "image/jpeg")],
            schema=VLMDiagnosis,
        )
        assert result.plant == "Tomato"  # markdown fences stripped before validation
        path, body, headers = api.requests[-1]
        assert path == "/openai/v1/chat/completions" and headers["Authorization"] == "Bearer groq-key"
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"][0]["role"] == "system" and "JSON Schema" in body["messages"][0]["content"]
        content = body["messages"][-1]["content"]
        assert content[0] == {"type": "text", "text": "photo"}
        assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")

        emb = OpenAICompatEmbedder("text-embedding-3-small", 768, api.url + "/v1", "k")
        assert emb.embed_query("x")[:2] == pytest.approx([0.0, 1.0])
        assert api.requests[-1][1]["dimensions"] == 768
    finally:
        api.close()


def test_openai_compatible_reasoning_effort_and_inline_thinking():
    def reply(path, body):
        answer = json.dumps({"answer": "Lower than yesterday [F1].", "cited": ["F1"]})
        return {"choices": [{"message": {"role": "assistant", "content": "<think>{draft: 1}</think>\n" + answer}}]}

    api = FakeAPI(reply)
    try:
        llm = OpenAICompatLLM("qwen3:8b", api.url + "/v1", "ollama", reasoning_effort="none")
        out = llm.structured(system="s", turns=[ChatTurn("user", "q")], schema=GroundedAnswer)
        assert out.cited == ["F1"]  # the braces inside <think> did not confuse JSON extraction
        assert api.requests[-1][1]["reasoning_effort"] == "none"
    finally:
        api.close()
