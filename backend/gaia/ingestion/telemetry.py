"""Telemetry intake: validate rover readings, store them, mark affected windows dirty.

Timestamps: the ESP32 has no RTC. It may send absolute ``ts`` (when NTP-synced)
or ``uptime_ms`` per reading plus ``sent_uptime_ms`` for the batch, in which
case the backend reconstructs wall-clock time from its own receive time.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..config import Settings
from ..core.sensors import TOGGLES, get_spec, normalize_key
from ..core.timeutil import floor_window, get_tz
from ..db.base import utcnow
from ..db.models import DirtyWindow, RoverHeartbeat, SensorReading, SensorStateEvent

MIN_VALID_TS = datetime(2020, 1, 1, tzinfo=timezone.utc)
MAX_FUTURE_SKEW = timedelta(minutes=5)


class RoverState(BaseModel):
    moving: bool | None = None
    arm_deployed: bool | None = None
    soil_valid: bool | None = Field(
        default=None, description="Firmware verdict that the soil probe is deployed and settled"
    )
    sensor_switches: dict[str, bool] | None = Field(
        default=None,
        description="Which sensors are switched on at this moment, e.g. {'dht11': true, 'mq5': false}",
    )


class ReadingIn(BaseModel):
    ts: datetime | float | None = Field(default=None, description="ISO-8601 with offset, or unix seconds / ms")
    uptime_ms: int | None = Field(default=None, ge=0)
    values: dict[str, float | None]
    state: RoverState | None = None


class TelemetryBatch(BaseModel):
    device_id: str = Field(pattern=r"^[A-Za-z0-9_.\-]{1,64}$")
    sent_uptime_ms: int | None = Field(default=None, ge=0)
    sensor_switches: dict[str, bool] | None = Field(
        default=None, description="Current sensor switches; a batch with no readings is a heartbeat"
    )
    readings: list[ReadingIn] = Field(max_length=5000)


@dataclass
class IngestResult:
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    windows_marked: int = 0
    warnings: list[str] = field(default_factory=list)
    switch_changes: list[dict] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        if len(self.warnings) < 20 and msg not in self.warnings:
            self.warnings.append(msg)

    def to_dict(self) -> dict:
        return {
            "accepted": self.accepted,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "windows_marked": self.windows_marked,
            "switches_changed": len(self.switch_changes),
            "warnings": self.warnings,
        }


def _resolve_ts(reading: ReadingIn, sent_uptime_ms: int | None, received_at: datetime) -> datetime | None:
    ts = reading.ts
    if isinstance(ts, (int, float)):
        seconds = ts / 1000 if ts > 1e11 else ts
        ts = datetime.fromtimestamp(seconds, tz=timezone.utc)
    if isinstance(ts, datetime):
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        if ts >= MIN_VALID_TS:
            return ts.astimezone(timezone.utc)
    if reading.uptime_ms is not None and sent_uptime_ms is not None:
        age_ms = sent_uptime_ms - reading.uptime_ms
        if age_ms >= 0:
            return received_at - timedelta(milliseconds=age_ms)
    if reading.ts is None and reading.uptime_ms is None:
        return received_at  # a single live reading forwarded without a clock
    return None


def _soil_valid(state: RoverState | None) -> bool:
    """The soil probe is only meaningful while parked with the arm lowered."""
    if state is None:
        return True
    if state.soil_valid is not None:
        return state.soil_valid
    if state.moving:
        return False
    return state.arm_deployed is not False


def _insert_ignore(
    session: Session,
    model,
    rows: list[dict],
    index_elements: list[str],
    update_cols: tuple[str, ...] = (),
    returning=None,
):
    """Bulk INSERT ... ON CONFLICT DO NOTHING (or DO UPDATE ``update_cols``)."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # pragma: no cover
        raise RuntimeError(f"Unsupported dialect {dialect}")
    stmt = insert(model)
    if update_cols:
        stmt = stmt.on_conflict_do_update(
            index_elements=index_elements, set_={c: stmt.excluded[c] for c in update_cols}
        )
    else:
        stmt = stmt.on_conflict_do_nothing(index_elements=index_elements)
    if returning is not None:
        stmt = stmt.returning(returning)
    return session.execute(stmt, rows)


def mark_windows_dirty(session: Session, keys: set[tuple[str, datetime]]) -> None:
    if not keys:
        return
    now = utcnow()
    rows = [{"device_id": d, "window_start": w, "marked_at": now} for d, w in keys]
    _insert_ignore(session, DirtyWindow, rows, ["device_id", "window_start"], update_cols=("marked_at",))


def _record_heartbeat(session: Session, device_id: str, received_at: datetime) -> None:
    """The rover is connected: one row per device per five-minute bucket, even for batches with no readings."""
    seconds = int(received_at.timestamp()) // 300 * 300
    bucket = datetime.fromtimestamp(seconds, tz=timezone.utc)
    _insert_ignore(
        session, RoverHeartbeat,
        [{"device_id": device_id, "bucket_start": bucket, "last_seen": received_at}],
        ["device_id", "bucket_start"], update_cols=("last_seen",),
    )


def _record_switches(
    session: Session, device_id: str, timeline: list[tuple[datetime, dict[str, bool]]], result: IngestResult
) -> None:
    """Store a switch event whenever a sensor's on/off state differs from the one before it."""
    last: dict[str, str | None] = {}
    for ts, switches in sorted(timeline, key=lambda item: item[0]):
        for raw, on in switches.items():
            toggle = raw.strip().lower().replace("-", "_")
            if toggle not in TOGGLES:
                result.warn(f"unknown sensor switch {raw!r}")
                continue
            state = "on" if on else "off"
            if toggle not in last:
                last[toggle] = session.scalar(
                    sa.select(SensorStateEvent.state)
                    .where(SensorStateEvent.device_id == device_id, SensorStateEvent.toggle == toggle,
                           SensorStateEvent.ts <= ts)
                    .order_by(SensorStateEvent.ts.desc())
                    .limit(1)
                )
            if last[toggle] == state:
                continue
            last[toggle] = state
            _insert_ignore(
                session, SensorStateEvent,
                [{"device_id": device_id, "toggle": toggle, "state": state, "ts": ts}],
                ["device_id", "toggle", "ts"],
            )
            result.switch_changes.append({"toggle": toggle, "state": state, "ts": ts.isoformat()})


def ingest_batch(
    session: Session, batch: TelemetryBatch, settings: Settings, received_at: datetime | None = None
) -> IngestResult:
    received_at = received_at or utcnow()
    tz = get_tz(settings.farm_timezone)
    result = IngestResult()
    rows: dict[tuple[str, datetime], dict] = {}
    timeline: list[tuple[datetime, dict[str, bool]]] = []
    _record_heartbeat(session, batch.device_id, received_at)

    for reading in batch.readings:
        ts = _resolve_ts(reading, batch.sent_uptime_ms, received_at)
        if ts is not None and reading.state and reading.state.sensor_switches:
            timeline.append((ts, reading.state.sensor_switches))
        n_values = len(reading.values)
        if ts is None:
            result.rejected += n_values
            result.warn("reading without a usable timestamp (send ts, or uptime_ms + sent_uptime_ms)")
            continue
        if ts > received_at + MAX_FUTURE_SKEW:
            result.rejected += n_values
            result.warn("reading timestamp is in the future; check the device clock")
            continue
        soil_ok = _soil_valid(reading.state)
        for raw_key, value in reading.values.items():
            key = normalize_key(raw_key)
            if key is None:
                result.rejected += 1
                result.warn(f"invalid sensor key {raw_key!r}")
                continue
            if value is None or not math.isfinite(value):
                result.rejected += 1
                continue
            spec = get_spec(key)
            if not spec.valid_min <= value <= spec.valid_max:
                result.rejected += 1
                result.warn(f"{key}={value} outside valid range [{spec.valid_min}, {spec.valid_max}]")
                continue
            if key == "soil_moisture" and not soil_ok:
                result.rejected += 1
                result.warn("soil_moisture dropped: probe not deployed or rover moving")
                continue
            rows[(key, ts)] = {
                "device_id": batch.device_id,
                "sensor": key,
                "ts": ts,
                "value": float(value),
                "received_at": received_at,
            }

    if batch.sensor_switches:
        timeline.append((received_at, batch.sensor_switches))
    if timeline:
        _record_switches(session, batch.device_id, timeline, result)

    if rows:
        inserted = _insert_ignore(
            session, SensorReading, list(rows.values()), ["device_id", "sensor", "ts"], returning=SensorReading.id
        )
        count = len(inserted.all())  # RETURNING yields only rows that did not conflict
        result.accepted = count
        result.duplicates = len(rows) - count
        windows = {(batch.device_id, floor_window(ts, settings.window_minutes, tz)) for (_, ts) in rows}
        mark_windows_dirty(session, windows)
        result.windows_marked = len(windows)
    return result
