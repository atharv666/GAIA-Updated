"""Sensor on/off history: what each sensor was doing during a time range, and why data is missing.

Built only from deterministic records, never guessed by the LLM:
  * switch events   (``sensor_state_events``)  - the farmer switched a sensor on or off
  * the readings    (``sensor_readings``)      - the sensor actually produced data
  * rover heartbeats (``rover_heartbeats``)    - the rover was connected, even with every sensor off

Resolution is five minutes. Every bucket of every sensor gets one status:

  recorded    the sensor produced readings
  off         it was switched off
  no_reading  it was switched on but produced nothing (heater warming up, probe not lowered, ...)
  offline     the rover was not connected at all
  unknown     no switch information exists for it
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..core.sensors import TOGGLE_ORDER, TOGGLES
from ..core.timeutil import day_bounds
from ..db.base import utcnow
from ..db.models import RoverHeartbeat, SensorReading, SensorStateEvent
from ..knowledge.store import ChunkDraft

BUCKET = timedelta(minutes=5)
RECORDED, OFF, NO_READING, OFFLINE, UNKNOWN = "recorded", "off", "no_reading", "offline", "unknown"
LONG_RANGE = timedelta(hours=36)  # beyond this, report totals instead of individual time spans


def floor_bucket(ts: datetime) -> datetime:
    seconds = int(ts.timestamp()) // 300 * 300
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


@dataclass(frozen=True)
class Span:
    start: datetime
    end: datetime
    status: str

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


# ---------------------------------------------------------------------------------------------
# Current state
# ---------------------------------------------------------------------------------------------


def primary_device(s: Session) -> str | None:
    """The rover whose switches matter: the one seen most recently."""
    for stmt in (
        sa.select(RoverHeartbeat.device_id).order_by(RoverHeartbeat.last_seen.desc()).limit(1),
        sa.select(SensorStateEvent.device_id).order_by(SensorStateEvent.ts.desc()).limit(1),
        sa.select(SensorReading.device_id).order_by(SensorReading.ts.desc()).limit(1),
    ):
        device = s.scalar(stmt)
        if device:
            return device
    return None


def has_switch_info(s: Session) -> bool:
    return s.scalar(sa.select(sa.func.count()).select_from(SensorStateEvent).limit(1)) not in (0, None)


def current_switches(s: Session, device_id: str | None = None) -> dict[str, tuple[str, datetime]]:
    """toggle -> (state, since) from the latest event of each switch."""
    device_id = device_id or primary_device(s)
    if device_id is None:
        return {}
    rows = s.execute(
        sa.select(SensorStateEvent.toggle, SensorStateEvent.state, SensorStateEvent.ts)
        .where(SensorStateEvent.device_id == device_id)
        .order_by(SensorStateEvent.ts)
    ).all()
    out: dict[str, tuple[str, datetime]] = {}
    previous: dict[str, str] = {}
    for toggle, state, ts in rows:  # "since" = when the current state began, not the last duplicate event
        if previous.get(toggle) != state:
            out[toggle] = (state, ts)
        previous[toggle] = state
    return out


def switch_state(s: Session, now: datetime | None = None, connected_within: timedelta = timedelta(seconds=90)) -> dict:
    """Current switch states for the app: which sensors are on or off, and whether the rover is connected."""
    now = now or utcnow()
    device = primary_device(s)
    seen = last_seen(s, device) if device else None
    current = current_switches(s, device) if device else {}
    return {
        "device_id": device,
        "last_seen": seen.isoformat() if seen else None,
        "connected": bool(seen and now - seen <= connected_within),
        "switches": [
            {"toggle": t, "label": TOGGLES[t].label, "short": TOGGLES[t].short, "sensors": list(TOGGLES[t].sensors),
             "state": current[t][0], "since": current[t][1].isoformat()}
            for t in TOGGLE_ORDER if t in current
        ],
    }


def last_seen(s: Session, device_id: str | None = None) -> datetime | None:
    stmt = sa.select(sa.func.max(RoverHeartbeat.last_seen))
    if device_id:
        stmt = stmt.where(RoverHeartbeat.device_id == device_id)
    return s.scalar(stmt)


# ---------------------------------------------------------------------------------------------
# Interval engine
# ---------------------------------------------------------------------------------------------


class Coverage:
    """Status of every sensor switch between ``start`` and ``end`` (clipped to now)."""

    def __init__(self, s: Session, device_id: str, start: datetime, end: datetime, now: datetime | None = None):
        self.device_id = device_id
        self.req_start = start
        self.end = min(end, now or utcnow())
        self.first = floor_bucket(start)
        self.events: dict[str, list[tuple[datetime, str]]] = {}
        self.seen: set[datetime] = set()
        self.recorded: dict[str, set[datetime]] = {}
        if self.end <= start:
            return
        for toggle, state, ts in s.execute(
            sa.select(SensorStateEvent.toggle, SensorStateEvent.state, SensorStateEvent.ts)
            .where(SensorStateEvent.device_id == device_id, SensorStateEvent.ts < self.end)
            .order_by(SensorStateEvent.ts)
        ).all():
            self.events.setdefault(toggle, []).append((ts, state))
        self.seen = {
            floor_bucket(b)
            for (b,) in s.execute(
                sa.select(RoverHeartbeat.bucket_start).where(
                    RoverHeartbeat.device_id == device_id,
                    RoverHeartbeat.bucket_start >= self.first,
                    RoverHeartbeat.bucket_start < self.end,
                )
            ).all()
        }
        self.seen |= {
            floor_bucket(ts)
            for (ts,) in s.execute(
                sa.select(SensorReading.ts)
                .where(SensorReading.device_id == device_id, SensorReading.ts >= self.first, SensorReading.ts < self.end)
                .distinct()
            ).all()
        }
        keys = {k: t for t, spec in TOGGLES.items() for k in spec.sensors}
        for sensor, ts in s.execute(
            sa.select(SensorReading.sensor, SensorReading.ts).where(
                SensorReading.device_id == device_id,
                SensorReading.sensor.in_(list(keys)),
                SensorReading.ts >= self.first,
                SensorReading.ts < self.end,
            )
        ).all():
            self.recorded.setdefault(keys[sensor], set()).add(floor_bucket(ts))

    # -- queries -----------------------------------------------------------------------------

    def known(self, toggle: str) -> bool:
        """Switch information exists for this sensor (it has reported its switch at least once)."""
        return bool(self.events.get(toggle))

    def state_at(self, toggle: str, ts: datetime) -> str | None:
        state = None
        for when, st in self.events.get(toggle, []):
            if when > ts:
                break
            state = st
        return state

    def connected(self, bucket: datetime) -> bool:
        return bucket in self.seen

    def status(self, toggle: str, bucket: datetime) -> str:
        if bucket in self.recorded.get(toggle, ()):
            return RECORDED
        if bucket not in self.seen:
            return OFFLINE
        state = self.state_at(toggle, bucket + BUCKET - timedelta(seconds=1))
        return {"off": OFF, "on": NO_READING}.get(state, UNKNOWN)

    def _buckets(self):
        b = self.first
        while b < self.end:
            yield b
            b += BUCKET

    def spans(self, toggle: str) -> list[Span]:
        out: list[Span] = []
        for b in self._buckets():
            st = self.status(toggle, b)
            start, end = max(b, self.req_start), min(b + BUCKET, self.end)
            if end <= start:
                continue
            if out and out[-1].status == st and out[-1].end == start:
                out[-1] = Span(out[-1].start, end, st)
            else:
                out.append(Span(start, end, st))
        return out

    def report_spans(self, toggle: str) -> list[Span]:
        """Spans for the farmer-facing text: consecutive "on" time is one span.

        A sensor that is on alternates between recorded and no_reading all the time when it only measures
        sometimes (the soil probe reads only while the rover is parked). That is normal, not a gap, so
        adjacent recorded / no_reading time is merged: the span counts as recorded if any reading exists in it.
        """
        out: list[Span] = []
        for sp in self.spans(toggle):
            if sp.status in (RECORDED, NO_READING) and out and out[-1].status in (RECORDED, NO_READING)                     and out[-1].end == sp.start:
                status = RECORDED if RECORDED in (out[-1].status, sp.status) else NO_READING
                out[-1] = Span(out[-1].start, sp.end, status)
            else:
                out.append(sp)
        return out

    def window_status(self, toggle: str) -> str | None:
        """One word for the whole range: off | partly_off | no_reading | None (nothing notable)."""
        statuses = {sp.status for sp in self.spans(toggle)}
        if statuses == {OFF}:
            return OFF
        if OFF in statuses and (RECORDED in statuses or NO_READING in statuses):
            return "partly_off"
        if statuses == {NO_READING}:
            return NO_READING
        return None


# ---------------------------------------------------------------------------------------------
# Plain-language descriptions
# ---------------------------------------------------------------------------------------------


def _duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def _spans_text(spans: list[Span], tz: ZoneInfo, limit: int = 6) -> str:
    multi_day = len({sp.start.astimezone(tz).date() for sp in spans} | {sp.end.astimezone(tz).date() for sp in spans}) > 1
    fmt = "%a %H:%M" if multi_day else "%H:%M"
    parts = [f"{sp.start.astimezone(tz):{fmt}}–{sp.end.astimezone(tz):{fmt}}" for sp in spans[:limit]]
    if len(spans) > limit:
        parts.append(f"and {len(spans) - limit} more")
    return ", ".join(parts)


def describe(cov: Coverage, toggle: str, tz: ZoneInfo) -> str | None:
    """Why a sensor has gaps in this range, or None when it simply recorded throughout (or has no info)."""
    spec = TOGGLES[toggle]
    spans = cov.report_spans(toggle)
    if not spans:
        return None
    by: dict[str, list[Span]] = {}
    for sp in spans:
        by.setdefault(sp.status, []).append(sp)
    if not cov.known(toggle) and RECORDED not in by:
        return None  # nothing is known about this sensor's switch
    if OFF not in by and NO_READING not in by:
        if RECORDED in by or OFFLINE not in by:
            return None
        return "no readings: the rover was not connected during this period"
    long_range = (cov.end - cov.req_start) > LONG_RANGE
    parts = []
    for status, label in ((RECORDED, "readings recorded"), (OFF, "switched off"),
                          (NO_READING, "switched on but no readings")):
        group = by.get(status)
        if not group:
            continue
        if long_range:
            parts.append(f"{label} for {_duration(sum(sp.minutes for sp in group))} in total")
        else:
            extra = f" ({spec.reason_no_reading})" if status == NO_READING else ""
            parts.append(f"{label} {_spans_text(group, tz)}{extra}")
    if OFFLINE in by:
        parts.append(f"rover not connected for {_duration(sum(sp.minutes for sp in by[OFFLINE]))} of the period")
    return "; ".join(parts)


def window_reasons(s: Session, device_id: str, window_start: datetime, window_end: datetime,
                   present: set[str]) -> dict[str, str]:
    """For a closed window: sensor key -> reason it has no statistics, for sensors with known switches."""
    cov = Coverage(s, device_id, window_start, window_end, now=window_end)
    reasons: dict[str, str] = {}
    for toggle, spec in TOGGLES.items():
        if not cov.known(toggle):
            continue
        missing = [k for k in spec.sensors if k not in present]
        if not missing:
            continue
        status = cov.window_status(toggle)
        if status == OFF:
            why = "sensor switched off"
        elif status == "partly_off":
            why = "sensor switched off for part of this window"
        elif status == NO_READING:
            why = f"sensor was on but had no reading ({spec.reason_no_reading})"
        else:
            continue
        for key in missing:
            reasons[key] = why
    return reasons


def day_lines(s: Session, device_id: str, day: date, tz: ZoneInfo, now: datetime | None = None) -> list[str]:
    """Daily-rollup lines about sensors that were off or silent for part of the day."""
    start, end = day_bounds(day, tz)
    cov = Coverage(s, device_id, start, end, now)
    lines = []
    for toggle in TOGGLE_ORDER:
        text = describe(cov, toggle, tz)
        if text and cov.known(toggle):
            lines.append(f"{TOGGLES[toggle].short}: {text}.")
    return lines


def sessions_document(s: Session, device_id: str, day: date, tz: ZoneInfo, now: datetime | None = None) -> ChunkDraft | None:
    """A short log of when the rover was connected and which sensors were on, for one day."""
    start, end = day_bounds(day, tz)
    cov = Coverage(s, device_id, start, end, now)
    toggles = [t for t in TOGGLE_ORDER if cov.known(t)]  # without switch events we would only be guessing
    if not toggles:
        return None
    sessions: list[tuple[datetime, datetime, tuple[str, ...], tuple[str, ...]]] = []
    for b in cov._buckets():
        if not cov.connected(b):
            continue
        on = tuple(t for t in toggles if cov.status(t, b) in (RECORDED, NO_READING))
        off = tuple(t for t in toggles if cov.status(t, b) == OFF)
        b_start, b_end = max(b, start), min(b + BUCKET, cov.end)
        if sessions and sessions[-1][2:] == (on, off) and sessions[-1][1] == b_start:
            sessions[-1] = (sessions[-1][0], b_end, on, off)
        else:
            sessions.append((b_start, b_end, on, off))
    if not sessions:
        return None
    names = lambda ts: ", ".join(TOGGLES[t].short for t in ts)  # noqa: E731
    lines = [f"Rover sensor on/off log for {day:%A %d %B %Y} from {device_id} (farm time):"]
    for a, b, on, off in sessions:
        what = []
        if on:
            what.append(f"sensors on: {names(on)}")
        if off:
            what.append(f"switched off: {names(off)}")
        if not on:
            what.append("rover connected with every sensor off (video only)")
        lines.append(f"- {a.astimezone(tz):%H:%M}–{b.astimezone(tz):%H:%M}: " + "; ".join(what) + ".")
    return ChunkDraft(
        title=f"Rover sensor sessions · {day:%a %d %b %Y} · {device_id}",
        content="\n".join(lines),
        time_start=sessions[0][0],
        time_end=sessions[-1][1],
        meta={
            "kind": "sessions",
            "device_id": device_id,
            "date": day.isoformat(),
            "sessions": [
                {"start": a.isoformat(), "end": b.isoformat(), "on": list(on), "off": list(off)}
                for a, b, on, off in sessions
            ],
        },
    )
