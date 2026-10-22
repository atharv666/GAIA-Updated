"""The demo loader drives the real API end to end (scripted models stand in for Gemini)."""

import importlib.util
import json
from argparse import Namespace
from pathlib import Path

DEMO = Path(__file__).resolve().parents[2] / "demo_data"


def _load_module():
    spec = importlib.util.spec_from_file_location("load_demo_data", DEMO / "load_demo_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _args(**kw):
    base = dict(device_key="test-device-key", api_key="", days=3, device_id="rover-01", step=60, seed=7,
                skip_diagnoses=False, diagnosis_timeout=60, pause=0.0, reindex=False)
    return Namespace(**{**base, **kw})


def _script_models(llm, vlm):
    vlm.handlers["VLMDiagnosis"] = lambda s, t: {
        "is_plant_image": True, "plant": "Tomato", "plant_part": "leaf", "health_status": "unhealthy",
        "diagnosis": "Possible early blight", "issue_categories": ["fungal_infection"],
        "evidence": ["Brown lesions with concentric rings"], "assumptions": [], "differential": ["Septoria leaf spot"],
        "severity": "moderate", "confidence": 0.7, "image_quality": "good",
    }
    steps = iter([{"rationale": "need treatment info", "queries": ["tomato early blight treatment"], "done": False},
                  {"rationale": "enough", "queries": [], "done": True}] * 8)
    llm.handlers["AgentStep"] = lambda s, t: next(steps)
    llm.handlers["AgentFindings"] = lambda s, t: {
        "headline": "Likely early blight [W1].", "recommended_actions": ["Remove infected leaves [W1]"],
        "cited_sources": ["W1"],
    }


def test_loader_populates_every_part_of_the_system_and_is_idempotent(client, llm, vlm):
    _script_models(llm, vlm)
    loader = _load_module()
    logs: list[str] = []
    first = loader.run(client, _args(), log=logs.append)

    assert first["values"] > 500 and first["notes"] == 5 and first["documents"] == 3 and first["diagnoses"] == 4
    by_type = first["status"]["knowledge_by_type"]
    assert {"sensor_window", "sensor_daily", "farm_note", "document", "diagnosis", "agent_finding"} <= set(by_type)
    assert by_type["farm_note"] == 5 and by_type["diagnosis"] == 4
    assert "sensor_sessions" in by_type  # the demo switches sensors on and off on purpose

    sessions = client.get("/api/v1/knowledge", params={"source_type": "sensor_sessions", "limit": 20}).json()
    assert any("switched off: soil probe" in c["content"] for c in sessions)  # yesterday evening
    state = client.get("/api/v1/telemetry/sensor-state").json()
    assert {s["toggle"]: s["state"] for s in state["switches"]} == {
        "dht11": "on", "soil_moisture": "on", "mq5": "on", "mq6": "on"}  # the live batch has everything on

    # The final batch uses the ESP32's no-clock form and lands "now", so latest readings are fresh.
    latest = {r["sensor"]: r for r in client.get("/api/v1/telemetry/latest").json()["readings"]}
    assert latest["temperature"]["age_s"] < 120 and "mq5" in latest and "mq6" in latest
    # Notes carry resolved relative dates (no raw template markers).
    notes = client.get("/api/v1/knowledge", params={"source_type": "farm_note", "limit": 50}).json()
    assert notes and not any("{{" in n["content"] for n in notes)

    # Re-running adds fresh sensor history but never duplicates notes, documents or diagnoses.
    second = loader.run(client, _args(), log=logs.append)
    assert second["notes"] == 0 and second["documents"] == 0 and second["diagnoses"] == 0
    assert second["status"]["knowledge_by_type"]["farm_note"] == 5
    assert second["status"]["knowledge_by_type"]["diagnosis"] == 4


def test_loader_skips_diagnoses_when_no_vlm_configured(client, container):
    from gaia.providers.llm import UnavailableLLM

    container.vlm = UnavailableLLM("GEMINI_API_KEY is not set")
    loader = _load_module()
    logs: list[str] = []
    out = loader.run(client, _args(), log=logs.append)
    assert out["diagnoses"] == 0 and out["notes"] == 5
    assert any("no vision-language model configured" in line for line in logs)


def test_sample_payloads_are_valid_telemetry(client):
    for path in sorted((DEMO / "sample_payloads").glob("esp32_post_*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        r = client.post("/api/v1/telemetry", json=payload, headers={"X-Device-Key": "test-device-key"})
        assert r.status_code == 200 and r.json()["accepted"] > 0, path.name
