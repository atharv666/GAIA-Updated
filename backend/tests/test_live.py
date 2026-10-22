"""Live view: activity events published by the pipeline, the state snapshot and the SSE stream."""

import json
from datetime import datetime

from gaia.core.activity import ACTIVITY

from .conftest import FIXED_NOW, IST, seed_series
from .test_pipeline_a import _seed_two_days  # noqa: F401


def _kinds(after: int) -> list[str]:
    return [e.kind for e in ACTIVITY.since(after, limit=400)]


def test_telemetry_window_chunk_and_storage_events_flow_in_order(client, container):
    start = ACTIVITY.last_id
    r = client.post(
        "/api/v1/telemetry",
        json={"device_id": "rover-01", "readings": [{"values": {"temperature": 27.5, "mq5": 230}},
                                                     {"values": {"temperature": 27.6, "mq5": 231}}]},
        headers={"X-Device-Key": "test-device-key"},
    )
    assert r.status_code == 200
    received = [e for e in ACTIVITY.since(start) if e.kind == "readings_received"]
    assert len(received) == 1
    assert received[0].data["readings"] == 2 and received[0].data["sensors"] == ["mq5", "temperature"]
    assert received[0].data["latest"] == {"temperature": 27.6, "mq5": 231.0}

    # An old, closed window gets aggregated, written as text, embedded and stored - in that order.
    seed_series(container, start_local=datetime(2025, 6, 15, 6, 0, tzinfo=IST), minutes=30,
                values=lambda i: {"humidity": 70, "temperature": 25})
    mark = ACTIVITY.last_id
    with container.session_factory():
        container.ingestion.sync(include_open=False, now=FIXED_NOW)
    events = ACTIVITY.since(mark, limit=400)
    kinds = [e.kind for e in events]
    first_window = kinds.index("window_closed")
    assert kinds[first_window:first_window + 4] == ["window_closed", "chunk_created", "chunk_embedded", "chunk_stored"]
    created = next(e for e in events if e.kind == "chunk_created" and e.data["source_type"] == "sensor_window")
    assert "Field sensor readings" in created.data["preview"]
    embedded = next(e for e in events if e.kind == "chunk_embedded")
    assert embedded.data["dims"] == container.settings.embedding_dim and embedded.data["model"]
    assert ACTIVITY.snapshot_counters()["chunks:sensor_window"] >= 2  # two 15-minute windows


def test_live_state_reports_window_progress_counters_and_events(client):
    client.post("/api/v1/telemetry", json={"device_id": "rover-01", "readings": [{"values": {"humidity": 61}}]},
                headers={"X-Device-Key": "test-device-key"})
    state = client.get("/api/v1/live/state").json()
    w = state["current_window"]
    assert w["readings"] >= 1 and 0 <= w["seconds_left"] <= 15 * 60
    assert datetime.fromisoformat(w["end"]) > datetime.fromisoformat(w["start"])
    assert state["window_minutes"] == 15 and state["embedder"].startswith("hashing")
    assert state["counters"]["readings"] >= 1 and state["last_event_id"] == state["events"][-1]["id"]
    assert any(e["kind"] == "readings_received" for e in state["events"])
    assert any(r["sensor"] == "humidity" for r in state["latest"])


def test_chat_and_diagnosis_publish_stage_events(client, llm, vlm):
    mark = ACTIVITY.last_id
    llm.handlers["GroundedAnswer"] = lambda s, t: {"answer": "Hello there.", "cited": []}
    llm.handlers["QueryPlan"] = lambda s, t: {"intent": "small_talk", "sensors": [], "time_ranges": [],
                                              "search_knowledge": False, "link_conditions": False,
                                              "search_query": "hello"}
    client.post("/api/v1/chat", json={"message": "hello", "mode": "farm"})
    kinds = _kinds(mark)
    assert kinds[0] == "chat_started" and "chat_stage" in kinds and kinds[-1] == "chat_finished"

    mark = ACTIVITY.last_id
    vlm.handlers["VLMDiagnosis"] = lambda s, t: {"is_plant_image": True, "plant": "Tomato", "health_status": "healthy",
                                                  "diagnosis": "No visible issue", "evidence": ["Even green colour"],
                                                  "confidence": 0.8, "image_quality": "good"}
    llm.handlers["AgentStep"] = lambda s, t: {"rationale": "enough", "queries": [], "done": True}
    llm.handlers["AgentFindings"] = lambda s, t: {"headline": "Looks healthy.", "cited_sources": []}
    from .conftest import jpeg_bytes
    r = client.post("/api/v1/diagnoses", files={"image": ("l.jpg", jpeg_bytes(), "image/jpeg")}, data={"source": "phone"})
    assert r.status_code == 202
    events = ACTIVITY.since(mark, limit=400)
    stages = [e.data["stage"] for e in events if e.kind == "diagnosis_stage"]
    assert stages == ["vlm", "searching", "writing"]
    done = [e for e in events if e.kind == "diagnosis_finished"][-1]
    assert done.data["status"] == "completed" and done.data["plant"] == "Tomato"
    assert {"chunk_created", "chunk_embedded", "chunk_stored"} <= {e.kind for e in events}


def test_live_stream_sends_hello_then_new_events(container):
    """Drive the SSE generator directly (TestClient cannot read an endless stream)."""
    import asyncio

    from gaia.api.live import live_stream

    class FakeRequest:
        def __init__(self):
            self.calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 3

    async def collect() -> str:
        mark = ACTIVITY.last_id
        resp = await live_stream(FakeRequest(), after=mark)
        ACTIVITY.publish("test_event", "hello from the test", n=1)
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk)
        return "".join(out)

    text = asyncio.run(collect())
    assert text.startswith("event: hello")
    block = next(b for b in text.split("\n\n") if "test_event" in b)
    assert "event: activity" in block
    payload = json.loads(block.split("data: ", 1)[1])
    assert payload["title"] == "hello from the test" and payload["data"] == {"n": 1}
