from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from gaia.db.models import DirtyWindow, KnowledgeChunk, SensorReading, SensorWindow
from gaia.ingestion.telemetry import TelemetryBatch, ingest_batch

from .conftest import FIXED_NOW, IST, seed_series


def _batch(**kw) -> TelemetryBatch:
    return TelemetryBatch.model_validate({"device_id": "rover-01", **kw})


def test_validation_soil_gating_and_uptime_timestamps(container):
    received = datetime(2025, 6, 15, 9, 0, tzinfo=timezone.utc)
    batch = _batch(
        sent_uptime_ms=100_000,
        readings=[
            {"uptime_ms": 40_000, "values": {"temp": 25.0, "soil_moisture": 40, "mq5": 210},
             "state": {"moving": True}},
            {"uptime_ms": 90_000, "values": {"humidity": 140, "soil_moisture": 41, "mq6": float("nan")},
             "state": {"moving": False, "arm_deployed": True}},
            {"ts": 0, "values": {"temperature": 30}},  # 1970 timestamp, no uptime: rejected
        ],
    )
    with container.session_factory() as s:
        r = ingest_batch(s, batch, container.settings, received_at=received)
        s.commit()
        rows = s.execute(sa.select(SensorReading.sensor, SensorReading.ts)).all()
    assert r.accepted == 3  # temperature (alias), mq5, soil_moisture from the parked reading
    assert r.rejected == 4  # soil while moving, humidity > 100, NaN, 1970 reading
    by_sensor = {sensor: ts for sensor, ts in rows}
    assert set(by_sensor) == {"temperature", "mq5", "soil_moisture"}
    assert by_sensor["temperature"] == received - timedelta(seconds=60)
    assert by_sensor["soil_moisture"] == received - timedelta(seconds=10)


def test_duplicates_are_ignored(container):
    start = datetime(2025, 6, 15, 6, 0, tzinfo=IST)
    seed_series(container, start_local=start, minutes=5, values=lambda i: {"temperature": 20 + i})
    with container.session_factory() as s:
        again = ingest_batch(
            s,
            _batch(readings=[{"ts": start.astimezone(timezone.utc).isoformat(), "values": {"temperature": 20}}]),
            container.settings,
        )
        s.commit()
        assert s.scalar(sa.select(sa.func.count()).select_from(SensorReading)) == 5
    assert again.duplicates == 1 and again.accepted == 0


def test_window_aggregation_documents_and_rollup(container):
    start = datetime(2025, 6, 15, 6, 0, tzinfo=IST)
    # 30 minutes -> two 15-minute windows. Soil 50..21 (one per minute), temperature constant 24.
    seed_series(container, start_local=start, minutes=30,
                values=lambda i: {"soil_moisture": 50 - i, "temperature": 24.0, "humidity": 88})
    report = container.ingestion.sync(now=FIXED_NOW)
    assert report.windows_indexed == 2 and report.days_rolled_up == 1 and not report.errors

    with container.session_factory() as s:
        w = s.scalars(sa.select(SensorWindow).where(SensorWindow.sensor == "soil_moisture")
                      .order_by(SensorWindow.window_start)).all()
        assert [x.count for x in w] == [15, 15]
        assert (w[0].min_value, w[0].max_value, w[0].avg_value) == (36, 50, 43)
        assert w[0].min_ts.astimezone(IST).minute == 14
        assert s.scalar(sa.select(sa.func.count()).select_from(DirtyWindow)) == 0

        chunk = s.scalar(sa.select(KnowledgeChunk).where(KnowledgeChunk.source_type == "sensor_window")
                         .order_by(KnowledgeChunk.time_start))
        assert "Soil moisture: average 43.0% (min 36.0%, max 50.0%, 15 readings)" in chunk.content
        assert "very humid" in chunk.content
        assert chunk.meta["day_part"] == "early morning"

        daily = s.scalar(sa.select(KnowledgeChunk).where(KnowledgeChunk.source_type == "sensor_daily"))
        assert "Data covers 06:00–06:30" in daily.content
        assert "daily average 35.5%" in daily.content  # mean of 50..21


def test_open_window_stays_dirty_and_late_data_reaggregates(container):
    start = datetime(2025, 6, 15, 14, 45, tzinfo=IST)  # window 14:45-15:00 still open at FIXED_NOW
    seed_series(container, start_local=start, minutes=5, values=lambda i: {"temperature": 30})
    report = container.ingestion.sync(now=FIXED_NOW - timedelta(minutes=5), include_open=True)
    assert report.open_windows == 1 and report.windows_indexed == 0
    with container.session_factory() as s:
        assert s.scalar(sa.select(sa.func.count()).select_from(DirtyWindow)) == 1
        assert s.scalar(sa.select(SensorWindow.count)) == 5

    # Later the window closes; more (late) readings arrive first.
    seed_series(container, start_local=start + timedelta(minutes=5), minutes=5, values=lambda i: {"temperature": 36})
    report = container.ingestion.sync(now=FIXED_NOW + timedelta(minutes=10))
    assert report.windows_indexed == 1
    with container.session_factory() as s:
        w = s.scalar(sa.select(SensorWindow))
        assert w.count == 10 and w.avg_value == 33 and w.max_value == 36
        assert s.scalar(sa.select(sa.func.count()).select_from(DirtyWindow)) == 0


def test_unchanged_window_is_not_reembedded(container):
    calls = {"n": 0}
    original = container.ingestion.embedder.embed_documents

    def counting(texts):
        calls["n"] += len(texts)
        return original(texts)

    container.ingestion.embedder.embed_documents = counting
    start = datetime(2025, 6, 15, 7, 0, tzinfo=IST)
    seed_series(container, start_local=start, minutes=15, values=lambda i: {"temperature": 25})
    container.ingestion.sync(now=FIXED_NOW)
    first = calls["n"]
    # Marking the window dirty again (e.g. re-posted duplicates) must not trigger re-embedding.
    with container.session_factory() as s:
        from gaia.ingestion.telemetry import mark_windows_dirty

        mark_windows_dirty(s, {("rover-01", start.astimezone(timezone.utc))})
        s.commit()
    container.ingestion.sync(now=FIXED_NOW)
    assert calls["n"] == first  # window chunk and daily rollup hashes unchanged
