from datetime import datetime

import pytest

from gaia.pipeline_a.planner import heuristic_plan
from gaia.pipeline_a.schemas import reconcile_citations
from gaia.providers.llm import ChatTurn, UnavailableLLM

from .conftest import FIXED_NOW, IST, seed_series


@pytest.mark.parametrize(
    ("question", "intent", "sensors", "ranges"),
    [
        ("How humid was my field this morning?", "sensor_statistics", ["humidity"], ["daypart:0:morning"]),
        ("What was the average soil moisture today?", "sensor_statistics", ["soil_moisture"], ["day:0"]),
        ("Was the soil moisture lower than yesterday?", "comparison", ["soil_moisture"], ["day:-1"]),
        ("What crop problems have been identified in my field?", "diagnosis_history", [], []),
        ("Any gas leaks right now?", "current_status", ["mq2", "mq5", "mq6"], ["now"]),
        ("When should I water?", "general_advice", ["soil_moisture", "humidity", "temperature"], ["last_hours:24"]),
        ("hello!", "small_talk", [], []),
        ("Where is the biogas unit?", "farm_knowledge", [], []),
        ("Which day was the hottest this week?", "sensor_statistics", ["temperature"], ["week:0"]),
    ],
)
def test_heuristic_planner(question, intent, sensors, ranges):
    plan = heuristic_plan(question, [])
    assert plan.intent == intent
    assert plan.sensors == sensors
    assert plan.time_ranges == ranges


def test_heuristic_planner_links_conditions_and_follow_ups():
    plan = heuristic_plan("What environmental conditions were recorded when this issue was detected?", [])
    assert plan.intent == "diagnosis_history" and plan.link_conditions
    follow = heuristic_plan("what about yesterday?", [ChatTurn("user", "average soil moisture today?")])
    assert follow.sensors == ["soil_moisture"] and follow.time_ranges == ["day:-1"]


def test_reconcile_citations_drops_hallucinated_ids():
    text, cited = reconcile_citations("Soil was 42% [F1] and falling [F9]. See [K1].", ["F1", "K7"], {"F1", "K1"})
    assert text == "Soil was 42% [F1] and falling. See [K1]."
    assert cited == ["F1", "K1"]


def _seed_two_days(container):
    # Yesterday 06:00-08:00 soil 50%; today 06:00-08:00 soil 40%.
    seed_series(container, start_local=datetime(2025, 6, 14, 6, 0, tzinfo=IST), minutes=120,
                values=lambda i: {"soil_moisture": 50.0, "humidity": 70, "temperature": 24})
    seed_series(container, start_local=datetime(2025, 6, 15, 6, 0, tzinfo=IST), minutes=120,
                values=lambda i: {"soil_moisture": 40.0 + (i % 2), "humidity": 80, "temperature": 26})


def test_farm_query_uses_exact_facts_and_validates_citations(container, llm):
    _seed_two_days(container)
    llm.handlers["QueryPlan"] = lambda sys, turns: {
        "intent": "comparison", "sensors": ["soil_moisture"], "time_ranges": ["day:0", "day:-1"],
        "search_knowledge": True, "link_conditions": False, "search_query": "soil moisture today vs yesterday",
    }
    captured = {}

    def answer(sys, turns):
        captured["prompt"] = turns[-1].content
        return {"answer": "Yes — it averaged 40.5% today vs 50.0% yesterday [F1][F2][F3]. Source [F42].",
                "cited": ["F3", "F42"], "used_general_knowledge": False, "insufficient_data": False}

    llm.handlers["GroundedAnswer"] = answer
    farm = container.chat.farm
    with container.session_factory() as s:
        result = farm.answer(s, "Was the soil moisture lower than yesterday?", [], now=FIXED_NOW)

    prompt = captured["prompt"]
    assert "[F1] Soil moisture today" in prompt and "average 40.5%" in prompt
    assert "[F2] Soil moisture yesterday" in prompt and "average 50.0%" in prompt
    assert "9.5 percentage points lower than yesterday" in prompt
    assert "(Daily sensor summary" in prompt  # daily rollups retrieved; window chunks skipped
    assert "(Sensor window summary" not in prompt
    assert "[F42]" not in result.answer
    assert [s.id for s in result.sources] == ["F1", "F2", "F3"]
    assert result.grounding["farm_data"] and not result.grounding["general_knowledge"]
    assert result.plan["planner"] == "llm"
    assert result.plan["ingestion"]["windows_indexed"] == 16  # ingestion ran before retrieval


def test_farm_query_reports_missing_data(container, llm):
    _seed_two_days(container)
    llm.handlers["GroundedAnswer"] = lambda sys, turns: {"answer": "No data [F1].", "cited": ["F1"],
                                                         "insufficient_data": True}
    # Planner LLM fails -> heuristic planner is used.
    with container.session_factory() as s:
        result = container.chat.farm.answer(s, "How humid was it last week?", [], now=FIXED_NOW)
    facts = [c for c in result.context if c.kind == "sensor_fact"]
    assert result.plan["planner"] == "heuristic"
    assert facts[0].snippet.startswith("No relative humidity readings were recorded last week")
    assert any(f.source_type == "sensor_coverage" for f in facts)


def test_farm_query_offline_without_llm(container):
    _seed_two_days(container)
    farm = container.chat.farm
    farm.llm = farm.planner.llm = UnavailableLLM("no key")
    with container.session_factory() as s:
        result = farm.answer(s, "What was the average soil moisture today?", [], now=FIXED_NOW)
    assert not result.llm_used
    assert "average 40.5%" in result.answer
    assert result.sources and result.sources[0].id == "F1"


def test_chat_service_persists_session_and_open_mode_cites_web(client, llm):
    llm.handlers["SearchPlan"] = lambda sys, turns: {"standalone_question": "How to prevent early blight?",
                                                     "needs_search": True, "queries": ["tomato early blight prevention"]}
    llm.handlers["GroundedAnswer"] = lambda sys, turns: {
        "answer": "Rotate crops and mulch [W1]. Fungicides may help [W2][W9].", "cited": ["W1", "W2"],
    }
    r = client.post("/api/v1/chat", json={"message": "How do I prevent early blight?", "mode": "open"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "[W9]" not in body["answer"]
    assert [s["id"] for s in body["sources"]] == ["W1", "W2"]
    assert body["sources"][0]["quality"] == "institutional"
    assert body["grounding"]["web"] is True

    session = client.get(f"/api/v1/chat/sessions/{body['session_id']}").json()
    assert [m["role"] for m in session["messages"]] == ["user", "assistant"]


def test_grouped_citations_are_split_and_validated():
    text, cited = reconcile_citations("Mulch helps [K1, K2]; spacing too [K3; F9].", [], {"K1", "K2", "K3"})
    assert text == "Mulch helps [K1][K2]; spacing too [K3]."
    assert cited == ["K1", "K2", "K3"]


def test_llm_plan_backstops(container, llm):
    llm.handlers["QueryPlan"] = lambda sys, turns: {
        "intent": "current_status", "sensors": ["humidity"], "time_ranges": ["daypart:0:morning"],
        "search_knowledge": False, "search_query": "humidity this morning",
    }
    planner = container.chat.farm.planner
    plan, used = planner.plan("How humid was my field this morning?", [], FIXED_NOW)
    assert used == "llm" and plan.intent == "sensor_statistics"

    llm.handlers["QueryPlan"] = lambda sys, turns: {
        "intent": "diagnosis_history", "sensors": [], "time_ranges": [], "search_knowledge": False,
        "link_conditions": False, "search_query": "conditions when issue detected",
    }
    plan, _ = planner.plan("What environmental conditions were recorded when this issue was detected?", [], FIXED_NOW)
    assert plan.search_knowledge and plan.link_conditions


def test_unusual_readings_become_their_own_fact(container, llm):
    # Baseline MQ5 around 200 ADC with a short spike to 520 ("high" band).
    seed_series(container, start_local=datetime(2025, 6, 15, 6, 0, tzinfo=IST), minutes=60,
                values=lambda i: {"mq5": 520 if 30 <= i < 33 else 200})
    llm.handlers["GroundedAnswer"] = lambda sys, turns: {"answer": "Spike [F2].", "cited": ["F2"]}
    with container.session_factory() as s:
        result = container.chat.farm.answer(s, "Any unusual MQ5 gas readings today?", [], now=FIXED_NOW)
    kinds = [c.source_type for c in result.context if c.kind == "sensor_fact"]
    assert "sensor_excursion" in kinds
    excursion = next(c for c in result.context if c.source_type == "sensor_excursion")
    assert excursion.snippet.startswith("UNUSUAL READING: MQ5 gas index") and "520 ADC" in excursion.snippet


def test_gas_questions_skip_sensors_that_are_not_installed(container, llm):
    # Rover fitted with MQ5 + MQ6 only: "gas" must not produce "no MQ2 data" facts.
    seed_series(container, start_local=datetime(2025, 6, 15, 14, 0, tzinfo=IST), minutes=60,
                values=lambda i: {"mq5": 210, "mq6": 190, "temperature": 28})
    captured = {}

    def answer(sys, turns):
        captured["prompt"] = turns[-1].content
        return {"answer": "Gas readings look normal [F1].", "cited": ["F1"]}

    llm.handlers["GroundedAnswer"] = answer
    llm.handlers["QueryPlan"] = lambda sys, turns: {
        "intent": "current_status", "sensors": ["mq2", "mq5", "mq6"], "time_ranges": ["last_hours:2"],
        "search_knowledge": False, "link_conditions": False, "search_query": "gas",
    }
    with container.session_factory() as s:
        result = container.chat.farm.answer(s, "Any gas leaks right now?", [], now=FIXED_NOW)
    assert "MQ5" in captured["prompt"] and "MQ6" in captured["prompt"]
    assert "MQ2" not in captured["prompt"]
    assert result.plan["sensors"] == ["mq5", "mq6"]
