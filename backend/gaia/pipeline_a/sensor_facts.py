"""Structured retrieval: exact statistics over the sensor log for a query plan.

Temporal / aggregate questions ("average soil moisture today", "lower than
yesterday?") are answered from SQL over the window aggregates, not from vector
similarity. Comparisons and deltas are computed here in code, so the LLM only
has to phrase numbers it is given.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ..core.sensors import TOGGLES, Band, get_spec, rag_sensor_keys, toggle_for_sensor
from ..core.timeutil import TimeRange, day_part, fmt_local
from ..db.base import utcnow
from ..db.models import SensorReading, SensorWindow
from ..ingestion.coverage import Coverage, current_switches, describe, has_switch_info, primary_device

STALE_AFTER = timedelta(minutes=30)


@dataclass
class Fact:
    id: str
    kind: str  # stats | comparison | profile | latest | no_data | coverage | linked
    title: str
    text: str
    sensor: str | None = None
    time_start: datetime | None = None
    time_end: datetime | None = None
    data: dict = field(default_factory=dict)


@dataclass
class RangeStats:
    sensor: str
    count: int
    windows: int
    avg: float
    min_value: float
    min_ts: datetime
    max_value: float
    max_ts: datetime
    latest_value: float
    latest_ts: datetime
    first_ts: datetime
    devices: list[str]

    def to_dict(self) -> dict:
        return {
            "sensor": self.sensor,
            "count": self.count,
            "windows": self.windows,
            "avg": round(self.avg, 4),
            "min": self.min_value,
            "min_ts": self.min_ts.isoformat(),
            "max": self.max_value,
            "max_ts": self.max_ts.isoformat(),
            "latest": self.latest_value,
            "latest_ts": self.latest_ts.isoformat(),
            "first_ts": self.first_ts.isoformat(),
            "devices": self.devices,
        }


def _windows(s: Session, sensor: str, start: datetime, end: datetime) -> list[SensorWindow]:
    return list(
        s.scalars(
            sa.select(SensorWindow)
            .where(SensorWindow.sensor == sensor, SensorWindow.window_start < end, SensorWindow.window_end > start)
            .order_by(SensorWindow.window_start)
        ).all()
    )


def combine(sensor: str, windows: list[SensorWindow]) -> RangeStats | None:
    windows = [w for w in windows if w.count > 0]
    if not windows:
        return None
    count = sum(w.count for w in windows)
    lo = min(windows, key=lambda w: w.min_value)
    hi = max(windows, key=lambda w: w.max_value)
    last = max(windows, key=lambda w: w.last_ts)
    return RangeStats(
        sensor=sensor,
        count=count,
        windows=len(windows),
        avg=sum(w.avg_value * w.count for w in windows) / count,
        min_value=lo.min_value,
        min_ts=lo.min_ts,
        max_value=hi.max_value,
        max_ts=hi.max_ts,
        latest_value=last.last_value,
        latest_ts=last.last_ts,
        first_ts=min(w.first_ts for w in windows),
        devices=sorted({w.device_id for w in windows}),
    )


def range_stats(s: Session, sensor: str, start: datetime, end: datetime) -> RangeStats | None:
    return combine(sensor, _windows(s, sensor, start, end))


class FactBuilder:
    def __init__(self, session: Session, tz: ZoneInfo, window_minutes: int):
        self.s = session
        self.tz = tz
        self.window_minutes = window_minutes
        self.facts: list[Fact] = []
        self._switches_loaded = False
        self._device: str | None = None
        self._current: dict[str, tuple[str, datetime]] = {}

    def _switch_state(self) -> dict[str, tuple[str, datetime]]:
        """Current on/off state of each sensor switch (empty when the rover never reported switches)."""
        if not self._switches_loaded:
            self._switches_loaded = True
            if has_switch_info(self.s):
                self._device = primary_device(self.s)
                self._current = current_switches(self.s, self._device)
        return self._current

    @staticmethod
    def _tip(toggles: list[str]) -> str:
        names = " and ".join(TOGGLES[t].short for t in toggles)
        warm = " Gas sensors need a few minutes to warm up after being switched on." if any(
            t.startswith("mq") for t in toggles) else ""
        return f" Tip: switch {names} on in the app (Rover tab) to record {'it' if len(toggles) == 1 else 'them'}.{warm}"

    def switch_fact(self, sensors: list[str], start: datetime, end: datetime, label: str, now: datetime) -> None:
        """Explain gaps: which requested sensors were switched off, silent, or the rover not connected."""
        if not self._switch_state() or self._device is None:
            return
        toggles = list(dict.fromkeys(t for t in (toggle_for_sensor(k) for k in sensors) if t))
        if not toggles:
            return
        cov = Coverage(self.s, self._device, start, end, now)
        lines, off_now = [], []
        for t in toggles:
            text = describe(cov, t, self.tz)
            if not text:
                continue
            lines.append(f"{TOGGLES[t].short}: {text}.")
            if self._current.get(t, ("",))[0] == "off":
                off_now.append(t)
        if not lines:
            return
        self._add(
            kind="switches", title=f"Sensor on/off history · {label}", time_start=start, time_end=end,
            text=f"Sensor on/off history {label} (farm time): " + " ".join(lines) + (self._tip(off_now) if off_now else ""),
            data={"toggles": toggles, "off_now": off_now},
        )

    def _add(self, **kw) -> Fact:
        fact = Fact(id=f"F{len(self.facts) + 1}", **kw)
        self.facts.append(fact)
        return fact

    def _t(self, ts: datetime, same_day: bool) -> str:
        return fmt_local(ts, self.tz, "%H:%M" if same_day else "%a %d %b %H:%M")

    # ------------------------------------------------------------------ facts

    def stats_fact(self, sensor: str, tr: TimeRange) -> RangeStats | None:
        spec = get_spec(sensor)
        st = range_stats(self.s, sensor, tr.start, tr.end)
        if st is None:
            self._add(
                kind="no_data", sensor=sensor, time_start=tr.start, time_end=tr.end,
                title=f"{spec.label} · {tr.label}",
                text=f"No {spec.label.lower()} readings were recorded {tr.label} ({tr.describe(self.tz)}).",
                data={"sensor": sensor, "range": tr.to_dict(self.tz)},
            )
            return None
        same_day = (tr.end - tr.start) <= timedelta(days=1)
        band = spec.band_for(st.avg)
        text = (
            f"{spec.label} {tr.label} ({tr.describe(self.tz)}): average {spec.with_unit(st.avg)}, "
            f"minimum {spec.with_unit(st.min_value)} at {self._t(st.min_ts, same_day)}, "
            f"maximum {spec.with_unit(st.max_value)} at {self._t(st.max_ts, same_day)}; "
            f"{st.count} readings in {st.windows} × {self.window_minutes}-min windows "
            f"between {self._t(st.first_ts, same_day)} and {self._t(st.latest_ts, same_day)}. "
            f"Most recent value in this period: {spec.with_unit(st.latest_value)} at {self._t(st.latest_ts, same_day)}."
        )
        if band:
            text += f" The average falls in the '{band.label}' band ({band.note})."
        # Excursions that the average hides (a short gas spike, a dry spell) become their own facts:
        # models reliably report a separate "unusual reading" line but often skip a clause in a long one.
        excursions: list[tuple[str, float, datetime, Band]] = []
        if spec.bands:
            hi_band, lo_band = spec.band_for(st.max_value), spec.band_for(st.min_value)
            if hi_band is spec.bands[-1] and hi_band is not band:
                text += f" Peak: {spec.with_unit(st.max_value)} ('{hi_band.label}' band), see the unusual-reading fact."
                excursions.append(("peaked at", st.max_value, st.max_ts, hi_band))
            if lo_band is spec.bands[0] and lo_band is not band and lo_band.label != "baseline":
                text += f" Low: {spec.with_unit(st.min_value)} ('{lo_band.label}' band), see the unusual-reading fact."
                excursions.append(("fell to", st.min_value, st.min_ts, lo_band))
        if spec.unit == "ADC":
            text += " (Uncalibrated relative gas index, not ppm.)"
        self._add(
            kind="stats", sensor=sensor, time_start=tr.start, time_end=tr.end,
            title=f"{spec.label} · {tr.label}", text=text,
            data={**st.to_dict(), "range": tr.to_dict(self.tz), "band": band.label if band else None},
        )
        for verb, value, at, xband in excursions:
            self._add(
                kind="excursion", sensor=sensor, time_start=tr.start, time_end=tr.end,
                title=f"Unusual {spec.label.lower()} · {tr.label}",
                text=(f"UNUSUAL READING: {spec.label} {verb} {spec.with_unit(value)} at "
                      f"{fmt_local(at, self.tz, '%a %d %b %H:%M')}, in the '{xband.label}' band ({xband.note}), "
                      f"even though the average {tr.label} was {spec.with_unit(st.avg)}."),
                data={"sensor": sensor, "value": value, "ts": at.isoformat(), "band": xband.label},
            )
        return st

    def comparison_fact(self, sensor: str, a: TimeRange, sa_: RangeStats, b: TimeRange, sb: RangeStats) -> None:
        spec = get_spec(sensor)
        delta = sa_.avg - sb.avg
        direction = "higher" if delta > 0 else "lower"
        significant = abs(delta) >= 10 ** (-spec.decimals) / 2
        verdict = f"{spec.delta_text(delta)} {direction} than" if significant else "about the same as"
        rel = f", i.e. {abs(delta) / abs(sb.avg) * 100:.0f}% {direction} in relative terms" if significant and sb.avg else ""
        text = (
            f"Comparison of {spec.label.lower()}: the average {a.label} ({spec.with_unit(sa_.avg)}) is "
            f"{verdict} {b.label} ({spec.with_unit(sb.avg)}){rel}. "
            f"Range {a.label}: {spec.with_unit(sa_.min_value)}–{spec.with_unit(sa_.max_value)}; "
            f"range {b.label}: {spec.with_unit(sb.min_value)}–{spec.with_unit(sb.max_value)}."
        )
        self._add(
            kind="comparison", sensor=sensor, time_start=min(a.start, b.start), time_end=max(a.end, b.end),
            title=f"{spec.label} · {a.label} vs {b.label}", text=text,
            data={"sensor": sensor, "a": a.to_dict(self.tz), "b": b.to_dict(self.tz),
                  "avg_a": sa_.avg, "avg_b": sb.avg, "delta": delta},
        )

    def profile_fact(self, sensor: str, tr: TimeRange) -> None:
        """Time-of-day profile (≤ 36 h) or per-day profile (longer ranges)."""
        spec = get_spec(sensor)
        windows = _windows(self.s, sensor, tr.start, tr.end)
        if len(windows) < 2:
            return
        buckets: dict[str, list[SensorWindow]] = defaultdict(list)
        span = tr.end - tr.start
        if span <= timedelta(hours=36):
            for w in windows:
                local = w.window_start.astimezone(self.tz)
                buckets[f"{local:%a} {day_part(local)}" if span > timedelta(hours=24) else day_part(local)].append(w)
            order = sorted(buckets, key=lambda k: min(w.window_start for w in buckets[k]))
            kind = "time of day"
        else:
            for w in windows:
                buckets[f"{w.window_start.astimezone(self.tz):%a %d %b}"].append(w)
            order = sorted(buckets, key=lambda k: min(w.window_start for w in buckets[k]))
            kind = "day"
        if len(order) < 2:
            return
        parts = []
        for key in order:
            st = combine(sensor, buckets[key])
            if st:
                parts.append(f"{key} {spec.with_unit(st.avg)}")
        first, last = combine(sensor, buckets[order[0]]), combine(sensor, buckets[order[-1]])
        direction = ""
        if first and last:
            d = last.avg - first.avg
            direction = (
                f" Overall it went from {spec.with_unit(first.avg)} to {spec.with_unit(last.avg)} "
                f"({'up' if d > 0 else 'down' if d < 0 else 'no change'} {spec.delta_text(d)})."
            )
        self._add(
            kind="profile", sensor=sensor, time_start=tr.start, time_end=tr.end,
            title=f"{spec.label} by {kind} · {tr.label}",
            text=f"{spec.label} {tr.label}, average by {kind}: " + ", ".join(parts) + "." + direction,
            data={"sensor": sensor, "range": tr.to_dict(self.tz), "buckets": order},
        )

    def latest_fact(self, sensors: list[str], now: datetime) -> None:
        lines, data, off_now = [], {}, []
        switches = self._switch_state()
        for sensor in sensors:
            spec = get_spec(sensor)
            toggle = toggle_for_sensor(sensor)
            off = switches.get(toggle) if toggle else None
            is_off = off is not None and off[0] == "off"
            if is_off and toggle not in off_now:
                off_now.append(toggle)
            row = self.s.execute(
                sa.select(SensorReading.value, SensorReading.ts, SensorReading.device_id)
                .where(SensorReading.sensor == sensor)
                .order_by(SensorReading.ts.desc())
                .limit(1)
            ).first()
            if row is None:
                since = f" since {fmt_local(off[1], self.tz)}" if is_off else ""
                lines.append(f"{spec.label}: " + (f"switched off{since}; no readings recorded yet." if is_off
                                                  else "no readings recorded yet."))
                continue
            value, ts, device = row
            age = now - ts
            band = spec.band_for(value)
            stale = f" — {_age_text(age)} old; the rover may be offline or parked elsewhere" if age > STALE_AFTER else ""
            if is_off:  # a deliberate switch-off explains the age better than "the rover may be offline"
                stale = f" — this sensor is switched off now (since {fmt_local(off[1], self.tz)})"
            lines.append(
                f"{spec.label}: {spec.with_unit(value)} at {fmt_local(ts, self.tz)} ({device})"
                + (f", '{band.label}'" if band else "") + stale + "."
            )
            data[sensor] = {"value": value, "ts": ts.isoformat(), "device_id": device, "age_s": int(age.total_seconds())}
        self._add(kind="latest", title="Latest sensor readings",
                  text="Latest readings — " + " ".join(lines) + (self._tip(off_now) if off_now else ""),
                  time_end=now, data={**data, "switched_off": off_now} if off_now else data)

    def coverage_fact(self) -> None:
        first, last, n = self.s.execute(
            sa.select(sa.func.min(SensorReading.ts), sa.func.max(SensorReading.ts), sa.func.count())
        ).one()
        if not n:
            text = "The sensor log is empty: no rover readings have been received yet."
        else:
            text = (
                f"The sensor log contains {n} readings from {fmt_local(first, self.tz, '%a %d %b %Y %H:%M')} "
                f"to {fmt_local(last, self.tz, '%a %d %b %Y %H:%M')} (farm time)."
            )
        self._add(kind="coverage", title="Sensor log coverage", text=text,
                  data={"count": n, "first": first.isoformat() if first else None, "last": last.isoformat() if last else None})

    def linked_conditions(self, label: str, at: datetime, sensors: list[str] | None = None) -> None:
        start, end = at - timedelta(hours=1), at + timedelta(hours=1)
        parts = []
        for sensor in sensors or rag_sensor_keys():
            st = range_stats(self.s, sensor, start, end)
            if st:
                parts.append(f"{get_spec(sensor).label.lower()} averaged {get_spec(sensor).with_unit(st.avg)} "
                             f"({get_spec(sensor).with_unit(st.min_value)}–{get_spec(sensor).with_unit(st.max_value)})")
        when = fmt_local(at, self.tz, "%a %d %b %Y %H:%M")
        text = (
            f"Sensor conditions within ±1 hour of {label} ({when}): " + "; ".join(parts) + "."
            if parts else f"No sensor readings were recorded within ±1 hour of {label} ({when})."
        )
        self._add(kind="linked", title=f"Conditions around {label}", text=text, time_start=start, time_end=end,
                  data={"at": at.isoformat()})
        if not parts:
            self.switch_fact(sensors or rag_sensor_keys(), start, end, f"around {label}", utcnow())


def _age_text(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    if minutes < 90:
        return f"{minutes} min"
    hours = minutes / 60
    return f"{hours:.0f} h" if hours < 48 else f"{hours / 24:.0f} days"


def build_sensor_facts(
    s: Session,
    *,
    intent: str,
    sensors: list[str],
    ranges: list[TimeRange],
    tz: ZoneInfo,
    window_minutes: int,
    now: datetime | None = None,
) -> list[Fact]:
    now = now or utcnow()
    fb = FactBuilder(s, tz, window_minutes)
    if not sensors:
        return []
    if intent in ("current_status", "general_advice") or any(r.expr == "now" for r in ranges):
        fb.latest_fact(sensors, now)
    stats: dict[tuple[str, int], RangeStats] = {}
    for i, tr in enumerate(ranges):
        if tr.expr == "now" and intent == "current_status":
            continue
        for sensor in sensors:
            st = fb.stats_fact(sensor, tr)
            if st:
                stats[(sensor, i)] = st
        fb.switch_fact(sensors, tr.start, tr.end, f"{tr.label} ({tr.describe(tz)})", now)
    if len(ranges) >= 2:
        for sensor in sensors:
            for j in range(1, len(ranges)):
                if (sensor, 0) in stats and (sensor, j) in stats:
                    fb.comparison_fact(sensor, ranges[0], stats[(sensor, 0)], ranges[j], stats[(sensor, j)])
    if intent in ("trend", "sensor_statistics", "general_advice") and len(ranges) == 1:
        tr = ranges[0]
        if tr.end - tr.start >= timedelta(hours=3):
            for sensor in sensors:
                if (sensor, 0) in stats:
                    fb.profile_fact(sensor, tr)
    if ranges and not stats and intent != "current_status":
        fb.coverage_fact()
    return fb.facts
