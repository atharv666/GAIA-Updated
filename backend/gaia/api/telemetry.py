"""Sensor telemetry intake and read APIs."""

from __future__ import annotations

from datetime import timedelta

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..container import Container
from ..core.activity import ACTIVITY
from ..core.sensors import SENSORS, TOGGLES, get_spec, public_registry
from ..core.timeutil import get_tz, resolve_time_expr
from ..db.base import utcnow
from ..db.models import SensorReading, SensorWindow
from ..ingestion.coverage import switch_state
from ..ingestion.telemetry import TelemetryBatch, ingest_batch
from ..pipeline_a.sensor_facts import range_stats
from .deps import get_container, get_db, require_api_key, require_device_key

router = APIRouter(tags=["telemetry"])


@router.post("/telemetry", dependencies=[Depends(require_device_key)])
def post_telemetry(batch: TelemetryBatch, s: Session = Depends(get_db), c: Container = Depends(get_container)):
    """Rover (or the app relaying rover data) pushes a batch of readings."""
    result = ingest_batch(s, batch, c.settings)
    s.commit()
    if result.accepted:
        sensors = sorted({k for r in batch.readings for k, v in r.values.items() if v is not None})
        latest = {k: v for k, v in (batch.readings[-1].values.items() if batch.readings else []) if v is not None}
        ACTIVITY.publish(
            "readings_received", f"Rover sent {len(batch.readings)} reading(s)",
            device_id=batch.device_id, readings=len(batch.readings), values=result.accepted, sensors=sensors,
            latest=latest, duplicates=result.duplicates, rejected=result.rejected,
        )
        ACTIVITY.count("readings", len(batch.readings))
    for change in result.switch_changes:
        spec = TOGGLES[change["toggle"]]
        ACTIVITY.publish(
            "sensor_switched", f"{spec.short} switched {change['state']}",
            device_id=batch.device_id, toggle=change["toggle"], state=change["state"], label=spec.label,
        )
    return result.to_dict()


@router.get("/sensors", dependencies=[Depends(require_api_key)])
def sensors():
    return public_registry()


def latest_readings(s: Session) -> list[dict]:
    latest = (
        sa.select(SensorReading.sensor, sa.func.max(SensorReading.ts).label("ts"))
        .group_by(SensorReading.sensor)
        .subquery()
    )
    rows = s.execute(
        sa.select(SensorReading.sensor, SensorReading.value, SensorReading.ts, SensorReading.device_id).join(
            latest, sa.and_(SensorReading.sensor == latest.c.sensor, SensorReading.ts == latest.c.ts)
        )
    ).all()
    now = utcnow()
    order = list(SENSORS)
    out, seen = [], set()
    for sensor, value, ts, device in sorted(rows, key=lambda r: (order.index(r[0]) if r[0] in order else 99, r[0])):
        if sensor in seen:
            continue
        seen.add(sensor)
        spec = get_spec(sensor)
        band = spec.band_for(value)
        out.append(
            {
                "sensor": sensor,
                "label": spec.label,
                "unit": spec.unit,
                "value": value,
                "display": spec.with_unit(value),
                "ts": ts.isoformat(),
                "age_s": int((now - ts).total_seconds()),
                "device_id": device,
                "band": band.label if band else None,
                "band_note": band.note if band else None,
                "progress": (value - spec.valid_min) / (spec.valid_max - spec.valid_min)
                if spec.valid_max > spec.valid_min else None,
                "rag": spec.rag,
            }
        )
    return out


@router.get("/telemetry/sensor-state", dependencies=[Depends(require_api_key)])
def telemetry_sensor_state(s: Session = Depends(get_db)):
    """Which sensors are switched on or off right now, and whether the rover is connected."""
    return switch_state(s)


@router.get("/telemetry/latest", dependencies=[Depends(require_api_key)])
def telemetry_latest(s: Session = Depends(get_db), c: Container = Depends(get_container)):
    return {"readings": latest_readings(s), "server_time": utcnow().isoformat()}


@router.get("/telemetry/history", dependencies=[Depends(require_api_key)])
def telemetry_history(
    sensor: str,
    hours: int = Query(24, ge=1, le=24 * 30),
    s: Session = Depends(get_db),
):
    """Window aggregates for charting (avg/min/max per window)."""
    since = utcnow() - timedelta(hours=hours)
    windows = s.scalars(
        sa.select(SensorWindow)
        .where(SensorWindow.sensor == sensor, SensorWindow.window_end > since)
        .order_by(SensorWindow.window_start)
    ).all()
    # Merge windows from several devices into one series.
    merged: dict = {}
    for w in windows:
        m = merged.setdefault(w.window_start, {"n": 0, "sum": 0.0, "min": w.min_value, "max": w.max_value})
        m["n"] += w.count
        m["sum"] += w.avg_value * w.count
        m["min"] = min(m["min"], w.min_value)
        m["max"] = max(m["max"], w.max_value)
    spec = get_spec(sensor)
    return {
        "sensor": sensor,
        "label": spec.label,
        "unit": spec.unit,
        "points": [
            {"t": t.isoformat(), "avg": m["sum"] / m["n"], "min": m["min"], "max": m["max"], "count": m["n"]}
            for t, m in sorted(merged.items())
        ],
    }


@router.get("/telemetry/summary", dependencies=[Depends(require_api_key)])
def telemetry_summary(
    range: str = Query("day:0", description="Time token, e.g. day:0, day:-1, last_hours:6"),
    s: Session = Depends(get_db),
    c: Container = Depends(get_container),
):
    tz = get_tz(c.settings.farm_timezone)
    tr = resolve_time_expr(range, utcnow(), tz)
    if tr is None:
        raise HTTPException(422, f"Invalid time token {range!r}")
    sensors = s.scalars(sa.select(SensorWindow.sensor).distinct()).all()
    stats = []
    for sensor in sorted(sensors):
        st = range_stats(s, sensor, tr.start, tr.end)
        if st:
            spec = get_spec(sensor)
            band = spec.band_for(st.avg)
            stats.append({**st.to_dict(), "label": spec.label, "unit": spec.unit,
                          "band": band.label if band else None, "display_avg": spec.with_unit(st.avg)})
    return {"range": tr.to_dict(tz), "stats": stats}
