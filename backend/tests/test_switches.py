"""Per-sensor on/off switches: recording, coverage, chunks, facts, the AI prompt and the API."""

from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from gaia.core.activity import ACTIVITY
from gaia.db.models import KnowledgeChunk, RoverHeartbeat, SensorStateEvent
from gaia.ingestion.coverage import Coverage, current_switches, describe, primary_device
from gaia.ingestion.telemetry import TelemetryBatch, ingest_batch
from gaia.pipeline_a.sensor_facts import build_sensor_facts
from gaia.core.timeutil import get_tz, resolve_time_expr

from .conftest import FIXED_NOW, IST

ALL_ON = {"dht11": True, "soil_moisture": True, "mq5": True, "mq6": True}
GAS_OFF = {"dht11": True, "soil_moisture": True, "mq5": False, "mq6": False}
ALL_OFF = {k: False for k in ALL_ON}
DAY = datetime(2025, 6, 15, tzinfo=IST)


def _at(h: int, m: int = 0) -> datetime:
    return DAY.replace(hour=h, minute=m).astimezone(timezone.utc)


def _post(container, start: datetime, minutes: int, switches: dict, values: dict) -> None:
    readings = [
        {"ts": (start + timedelta(minutes=i)).isoformat(), "values": values, "state": {"sensor_switches": switches}}
        for i in range(minutes)
    ]
    with container.session_factory() as s:
        ingest_batch(s, TelemetryBatch.model_validate({"device_id": "rover-01", "readings": readings}),
                     container.settings, received_at=start + timedelta(minutes=minutes - 1, seconds=1))
        s.commit()


def _heartbeat(container, at: datetime, switches: dict) -> dict:
    with container.session_factory() as s:
        result = ingest_batch(
            s, TelemetryBatch.model_validate({"device_id": "rover-01", "readings": [], "sensor_switches": switches}),
            container.settings, received_at=at)
        s.commit()
    return result.to_dict()


def _seed_day(container) -> None:
    """06:00-07:00 everything on; 07:00-08:00 gas off; rover away; 12:00-12:30 connected, all sensors off."""
    _post(container, _at(6), 60, ALL_ON, {"temperature": 24.0, "humidity": 70, "mq5": 210, "mq6": 190})
    _post(container, _at(7), 60, GAS_OFF, {"temperature": 25.0, "humidity": 66})
    for i in range(0, 30, 5):  # heartbeats only: video-only mode
        _heartbeat(container, _at(12, i), ALL_OFF)
    container.ingestion.sync(include_open=False, now=FIXED_NOW)


def test_switch_events_are_stored_only_on_change(container):
    _seed_day(container)
    with container.session_factory() as s:
        events = s.execute(
            sa.select(SensorStateEvent.toggle, SensorStateEvent.state, SensorStateEvent.ts)
            .where(SensorStateEvent.toggle == "mq5").order_by(SensorStateEvent.ts)
        ).all()
        assert [(t, st) for t, st, _ in events] == [("mq5", "on"), ("mq5", "off")]
        assert events[0][2] == _at(6) and events[1][2] == _at(7)
        assert current_switches(s)["mq5"] == ("off", _at(7))
        assert s.scalar(sa.select(sa.func.count()).select_from(RoverHeartbeat)) >= 6


def test_heartbeat_only_batch_is_accepted_and_reports_switch_changes(client):
    mark = ACTIVITY.last_id
    r = client.post("/api/v1/telemetry", json={"device_id": "rover-01", "readings": [],
                                               "sensor_switches": {"mq5": True, "mq6": False, "bogus": True}},
                    headers={"X-Device-Key": "test-device-key"})
    body = r.json()
    assert r.status_code == 200 and body["accepted"] == 0 and body["switches_changed"] == 2
    assert any("bogus" in w for w in body["warnings"])
    state = client.get("/api/v1/telemetry/sensor-state").json()
    assert state["connected"] is True and state["device_id"] == "rover-01"
    assert {s["toggle"]: s["state"] for s in state["switches"]} == {"mq5": "on", "mq6": "off"}

    kinds = [(e.kind, e.data.get("toggle"), e.data.get("state")) for e in ACTIVITY.since(mark)]
    assert ("sensor_switched", "mq6", "off") in kinds and ("sensor_switched", "mq5", "on") in kinds
    # The same state again is not a change.
    r = client.post("/api/v1/telemetry", json={"device_id": "rover-01", "readings": [],
                                               "sensor_switches": {"mq5": True, "mq6": False}},
                    headers={"X-Device-Key": "test-device-key"})
    assert r.json()["switches_changed"] == 0
    live = client.get("/api/v1/live/state").json()
    assert {s["toggle"] for s in live["sensor_switches"]["switches"]} == {"mq5", "mq6"}


def test_coverage_statuses_per_sensor(container):
    _seed_day(container)
    tz = get_tz("Asia/Kolkata")
    with container.session_factory() as s:
        cov = Coverage(s, "rover-01", _at(6), _at(9), FIXED_NOW)
        mq5 = [(sp.start, sp.end, sp.status) for sp in cov.spans("mq5")]
        assert mq5 == [(_at(6), _at(7), "recorded"), (_at(7), _at(8), "off"), (_at(8), _at(9), "offline")]
        # Soil was switched on all the time but the probe never produced readings (rover moving).
        assert {sp.status for sp in cov.spans("soil_moisture")} == {"no_reading", "offline"}
        text = describe(cov, "mq5", tz)
        assert "readings recorded 06:00–07:00" in text and "switched off 07:00–08:00" in text
        assert "rover not connected for 1 h" in text
        assert "probe was not lowered" in describe(cov, "soil_moisture", tz)
        assert describe(cov, "dht11", tz) is None  # recorded for the whole connected time: nothing to explain

        video_only = Coverage(s, "rover-01", _at(12), _at(12, 30), FIXED_NOW)
        assert {sp.status for sp in video_only.spans("mq6")} == {"off"}

        long_range = Coverage(s, "rover-01", _at(0) - timedelta(days=3), _at(23), FIXED_NOW)
        assert "in total" in describe(long_range, "mq5", tz)


def test_chunks_explain_missing_sensors(container):
    _seed_day(container)
    with container.session_factory() as s:
        windows = {c.source_id: c for c in s.scalars(sa.select(KnowledgeChunk).where(
            KnowledgeChunk.source_type == "sensor_window"))}
        gas_off = next(c for c in windows.values() if c.title.startswith("Sensor window · Sun 15 Jun 2025 07:00"))
        assert "MQ5 gas index (LPG / natural gas / methane): not recorded in this window - sensor switched off." \
            in gas_off.content
        assert "mq5" in gas_off.meta["not_recorded"]
        # While gas was on, the soil probe was switched on but silent, and the window says so.
        gas_on = next(c for c in windows.values() if c.title.startswith("Sensor window · Sun 15 Jun 2025 06:00"))
        assert "not recorded in this window - sensor was on but had no reading" in gas_on.content

        daily = s.scalar(sa.select(KnowledgeChunk).where(KnowledgeChunk.source_type == "sensor_daily"))
        assert "Sensor on/off during the day:" in daily.content
        assert "MQ5: readings recorded 06:00–07:00; switched off 07:00–08:00, 12:00–12:30" in daily.content

        sessions = s.scalar(sa.select(KnowledgeChunk).where(KnowledgeChunk.source_type == "sensor_sessions"))
        text = sessions.content
        assert "06:00–07:00: sensors on: DHT11, soil probe, MQ5, MQ6." in text
        assert "07:00–08:00: sensors on: DHT11, soil probe; switched off: MQ5, MQ6." in text
        assert "12:00–12:30: switched off: DHT11, soil probe, MQ5, MQ6; rover connected with every sensor off" in text


def test_facts_give_reasons_and_a_tip(container):
    _seed_day(container)
    tz = get_tz("Asia/Kolkata")
    tr = resolve_time_expr("day:0", FIXED_NOW, tz)
    with container.session_factory() as s:
        facts = build_sensor_facts(s, intent="sensor_statistics", sensors=["mq5"], ranges=[tr], tz=tz,
                                   window_minutes=15, now=FIXED_NOW)
        by_kind = {f.kind: f for f in facts}
        assert "average 210 ADC" in by_kind["stats"].text
        sw = by_kind["switches"].text
        assert "MQ5: readings recorded 06:00–07:00; switched off 07:00–08:00, 12:00–12:30" in sw
        assert "Tip: switch MQ5 on in the app (Rover tab)" in sw and "warm up" in sw

        latest = build_sensor_facts(s, intent="current_status", sensors=["mq5", "temperature"],
                                    ranges=[resolve_time_expr("now", FIXED_NOW, tz)], tz=tz, window_minutes=15,
                                    now=FIXED_NOW)[0]
        assert "MQ5 gas index (LPG / natural gas / methane): 210 ADC" in latest.text
        assert "this sensor is switched off now (since Sun 15 Jun 07:00)" in latest.text  # MQ5
        assert "this sensor is switched off now (since Sun 15 Jun 12:00)" in latest.text  # DHT11: video-only mode
        assert "Tip: switch MQ5 and DHT11 on in the app" in latest.text


def test_no_data_fact_is_followed_by_the_reason(container):
    _seed_day(container)
    tz = get_tz("Asia/Kolkata")
    tr = resolve_time_expr("daypart:0:afternoon", FIXED_NOW, tz)  # 12:00-17:00: only the video-only heartbeats
    with container.session_factory() as s:
        facts = build_sensor_facts(s, intent="sensor_statistics", sensors=["mq6"], ranges=[tr], tz=tz,
                                   window_minutes=15, now=FIXED_NOW)
    kinds = [f.kind for f in facts]
    assert "no_data" in kinds and "switches" in kinds
    assert "switched off 12:00–12:30" in next(f for f in facts if f.kind == "switches").text


def test_the_prompt_carries_the_reasons_to_the_model(container, llm):
    _seed_day(container)
    captured = {}
    llm.handlers["QueryPlan"] = lambda sys, turns: {
        "intent": "sensor_statistics", "sensors": ["mq5"], "time_ranges": ["day:0"], "search_knowledge": False,
        "link_conditions": False, "search_query": "mq5 today",
    }

    def answer(sys, turns):
        captured["system"], captured["prompt"] = sys, turns[-1].content
        return {"answer": "MQ5 was recorded 06:00-07:00 only; it was switched off afterwards [F2].", "cited": ["F2"]}

    llm.handlers["GroundedAnswer"] = answer
    with container.session_factory() as s:
        result = container.chat.farm.answer(s, "What was the MQ5 average today?", [], now=FIXED_NOW)
    assert "switched off 07:00–08:00, 12:00–12:30" in captured["prompt"] and "Tip: switch MQ5 on" in captured["prompt"]
    assert "Sensors can be switched on and off" in captured["system"]
    assert [s.kind for s in result.sources] == ["sensor_fact"]


def test_legacy_telemetry_without_switches_behaves_as_before(container):
    from .conftest import seed_series

    seed_series(container, start_local=datetime(2025, 6, 15, 6, 0, tzinfo=IST), minutes=30,
                values=lambda i: {"humidity": 70, "mq5": 210})
    container.ingestion.sync(include_open=False, now=FIXED_NOW)
    tz = get_tz("Asia/Kolkata")
    with container.session_factory() as s:
        assert current_switches(s) == {}
        assert s.scalar(sa.select(sa.func.count()).select_from(KnowledgeChunk).where(
            KnowledgeChunk.source_type == "sensor_sessions")) == 0
        facts = build_sensor_facts(s, intent="sensor_statistics", sensors=["mq5"],
                                   ranges=[resolve_time_expr("day:0", FIXED_NOW, tz)], tz=tz, window_minutes=15,
                                   now=FIXED_NOW)
        assert "switches" not in {f.kind for f in facts}
        assert primary_device(s) == "rover-01"


def test_a_sensor_that_measures_only_sometimes_is_not_reported_as_fragmented(container):
    """The soil probe reads only while parked (5 min of every 10): that is normal, not a gap."""
    readings = []
    for i in range(120):
        ts = _at(6) + timedelta(minutes=i)
        values = {"temperature": 24.0, "humidity": 70}
        if i % 10 >= 5:
            values["soil_moisture"] = 40.0 + (i % 3)
        readings.append({"ts": ts.isoformat(), "values": values, "state": {"sensor_switches": ALL_ON}})
    with container.session_factory() as s:
        ingest_batch(s, TelemetryBatch.model_validate({"device_id": "rover-01", "readings": readings}),
                     container.settings, received_at=_at(7, 59))
        s.commit()
    tz = get_tz("Asia/Kolkata")
    with container.session_factory() as s:
        cov = Coverage(s, "rover-01", _at(6), _at(8), FIXED_NOW)
        assert len(cov.spans("soil_moisture")) > 3  # raw buckets alternate between recorded and no_reading
        merged = cov.report_spans("soil_moisture")
        assert [(m.start, m.end, m.status) for m in merged] == [(_at(6), _at(8), "recorded")]
        assert describe(cov, "soil_moisture", tz) is None
