from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import event

from gaia.db.models import KnowledgeChunk

from .conftest import FIXED_NOW, IST, jpeg_bytes, seed_series

UNHEALTHY = {
    "is_plant_image": True,
    "plant": "Tomato",
    "plant_part": "leaf",
    "health_status": "unhealthy",
    "diagnosis": "Possible early blight",
    "issue_categories": ["fungal_infection", "disease"],
    "evidence": ["Brown lesions with concentric rings on older leaves", "Yellow halo around lesions"],
    "assumptions": ["Lesions are not caused by spray injury"],
    "differential": ["Septoria leaf spot"],
    "severity": "moderate",
    "confidence": 0.78,
    "image_quality": "good",
}


def _script_agent(llm, rounds: list[dict]):
    steps = iter(rounds)
    llm.handlers["AgentStep"] = lambda sys, turns: next(steps)
    llm.handlers["AgentFindings"] = lambda sys, turns: {
        "headline": "Likely early blight; act early to limit spread [W1].",
        "condition_overview": "A fungal disease of tomato caused by Alternaria [W1].",
        "symptoms_to_check": ["Target-like brown spots on older leaves [W1]"],
        "possible_causes": ["Warm, humid weather and leaf wetness [W1]"],
        "recommended_actions": ["Remove infected lower leaves [W1]", "Consider a labelled fungicide [W2] [W7]"],
        "preventive_measures": ["Mulch and rotate crops [W1]"],
        "when_to_consult_expert": "If more than a quarter of leaves are affected.",
        "cited_sources": ["W1", "W2", "W7"],
    }


def _post_image(client, **params):
    return client.post(
        "/api/v1/diagnoses",
        params=params,
        files={"image": ("leaf.jpg", jpeg_bytes(), "image/jpeg")},
        data={"source": "phone", "note": "north plot, row 3"},
    )


def test_diagnosis_pipeline_end_to_end(client, container, llm, vlm, search):
    vlm.handlers["VLMDiagnosis"] = lambda sys, turns: UNHEALTHY
    _script_agent(llm, [
        {"rationale": "Need treatment info", "queries": ["tomato early blight treatment"], "done": False},
        {"rationale": "Enough", "queries": [], "done": True},
    ])

    statements: list[str] = []

    def capture(conn, cursor, statement, *args):
        statements.append(statement.lower())

    event.listen(container.engine, "before_cursor_execute", capture)
    try:
        r = _post_image(client, wait="true")
    finally:
        event.remove(container.engine, "before_cursor_execute", capture)

    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "completed" and d["stage"] == "done"
    assert d["analysis"]["diagnosis"] == "Possible early blight"
    assert "not a statistically calibrated probability" in d["confidence_note"]
    assert d["web_grounded"] is True
    assert [s["id"] for s in d["sources"] if s["cited"]] == ["W1", "W2"]
    assert "[W7]" not in " ".join(d["findings"]["recommended_actions"])
    assert d["agent_trace"][0]["queries"] == ["tomato early blight treatment"]
    assert search.queries == ["tomato early blight treatment"]
    assert d["recapture_tips"] == []  # confident and good quality

    # Constraint 2: Pipeline B never touches sensor data or chat history.
    assert not any("sensor_" in st or "chat_" in st for st in statements)

    # Knowledge augmentation: diagnosis + agent findings written to the shared store.
    with container.session_factory() as s:
        chunks = s.scalars(sa.select(KnowledgeChunk).where(KnowledgeChunk.source_id == d["id"])).all()
    types = sorted(c.source_type for c in chunks)
    assert types == ["agent_finding", "diagnosis"]
    finding = next(c for c in chunks if c.source_type == "agent_finding")
    assert "(extension.umn.edu)" in finding.content and "[W1]" not in finding.content
    assert finding.meta["sources"][0]["url"].startswith("https://extension.umn.edu")

    image = client.get(d["image_url"])
    assert image.status_code == 200 and image.headers["content-type"] == "image/jpeg"


def test_pipeline_a_reads_pipeline_b_knowledge(client, container, llm, vlm):
    vlm.handlers["VLMDiagnosis"] = lambda sys, turns: UNHEALTHY
    _script_agent(llm, [{"rationale": "search", "queries": ["tomato early blight"], "done": True}])
    diag = _post_image(client, wait="true").json()
    seed_series(container, start_local=datetime(2025, 6, 15, 6, 0, tzinfo=IST), minutes=60,
                values=lambda i: {"humidity": 90, "temperature": 27})

    captured = {}

    def answer(sys, turns):
        captured["prompt"] = turns[-1].content
        return {"answer": "A possible early blight was found on tomato [K1].", "cited": ["K1"]}

    llm.handlers["GroundedAnswer"] = answer
    with container.session_factory() as s:
        result = container.chat.farm.answer(s, "What crop problems have been identified in my field?", [],
                                            now=FIXED_NOW)
    assert "Crop diagnosis · Tomato · unhealthy" in captured["prompt"]
    assert result.sources[0].source_type in ("diagnosis", "agent_finding")
    assert result.sources[0].meta["diagnosis_id"] == diag["id"]
    assert result.grounding["knowledge_store"] is True


def test_uncertain_gets_recapture_tips_and_unhealthy_branch(client, llm, vlm):
    vlm.handlers["VLMDiagnosis"] = lambda sys, turns: {**UNHEALTHY, "health_status": "uncertain", "confidence": 0.4,
                                                       "image_quality": "poor"}
    seen = {}

    def step(sys, turns):
        seen["state"] = turns[-1].content
        return {"rationale": "check look-alikes", "queries": [], "done": False}

    llm.handlers["AgentStep"] = step
    llm.handlers["AgentFindings"] = lambda sys, turns: {"headline": "Needs confirmation [W1]."}
    d = _post_image(client, wait="true").json()
    assert d["status"] == "completed"
    assert "uncertain - treat like unhealthy" in seen["state"]
    assert d["recapture_tips"]
    # The planner returned no queries, so deterministic fallback queries were used.
    assert any("vs Septoria leaf spot" in q for q in d["agent_trace"][0]["queries"])


def test_not_a_plant_skips_agent_and_knowledge(client, container, llm, vlm):
    vlm.handlers["VLMDiagnosis"] = lambda sys, turns: {**UNHEALTHY, "is_plant_image": False, "plant": "Unknown"}
    d = _post_image(client, wait="true").json()
    assert d["status"] == "not_plant" and d["analysis"]["health_status"] == "uncertain"
    assert "AgentStep" not in llm.schemas_called()
    with container.session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(KnowledgeChunk)) == 0


def test_async_job_and_failures(client, container, vlm):
    r = client.post("/api/v1/diagnoses", files={"image": ("x.jpg", b"not an image", "image/jpeg")})
    assert r.status_code == 422

    # No VLM handler -> the job fails and ends in a terminal state.
    r = _post_image(client)
    assert r.status_code == 202 and r.json()["status"] == "processing"
    d = client.get(f"/api/v1/diagnoses/{r.json()['id']}").json()  # background task ran after the response
    assert d["status"] == "failed" and "VLMDiagnosis" in d["error"]

    listing = client.get("/api/v1/diagnoses").json()
    assert listing[0]["id"] == d["id"]
    assert client.delete(f"/api/v1/diagnoses/{d['id']}").status_code == 200
    assert client.get(f"/api/v1/diagnoses/{d['id']}").status_code == 404


def test_vlm_unavailable_returns_503(client, container):
    from gaia.providers.llm import UnavailableLLM

    container.vlm = UnavailableLLM("no key")
    assert _post_image(client).status_code == 503


def test_findings_drop_placeholders_and_fake_citations():
    from gaia.pipeline_b.agent import AgentFindings, _clean_citations

    raw = AgentFindings(
        headline="Possible leaf spot [VLM].",
        care_guidance=["N/A", "n/a.", "None", "Not applicable"],
        recommended_actions=["Use neem [W2, W3]", "Thin foliage [W9]"],
        when_to_consult_expert="N/A",
    )
    clean, cited = _clean_citations(raw, {"W2", "W3"})
    assert clean.headline == "Possible leaf spot."
    assert clean.care_guidance == [] and clean.when_to_consult_expert == ""
    assert clean.recommended_actions == ["Use neem [W2][W3]", "Thin foliage"]
    assert cited == ["W2", "W3"]
