"""Turn sensor aggregates into retrieval documents.

Layer 1  sensor_window      deterministic text for one window (e.g. 15 min)
Layer 2  sensor_daily       deterministic daily rollup: extremes, day-part profile,
                            threshold events
Layer 3  sensor_digest_llm  OPTIONAL LLM narrative written only from the Layer 2
                            text and labelled as AI interpretation

Every number that reaches the farmer comes from Layers 1-2 or from exact SQL
statistics; Layer 3 only adds semantic phrasing that helps retrieval.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..core.sensors import get_spec
from ..core.timeutil import day_part, day_part_order, fmt_local
from ..db.models import SensorWindow
from ..knowledge.store import ChunkDraft
from ..providers.llm import LLM, ChatTurn


def _sensor_order(keys) -> list[str]:
    order = ["temperature", "humidity", "soil_moisture", "mq2", "mq5", "mq6"]
    return sorted(keys, key=lambda k: (order.index(k) if k in order else 99, k))


def window_source_id(device_id: str, window_start: datetime) -> str:
    return f"{device_id}:{window_start.strftime('%Y-%m-%dT%H:%MZ')}"


def window_document(
    device_id: str, windows: list[SensorWindow], tz: ZoneInfo, reasons: dict[str, str] | None = None
) -> ChunkDraft | None:
    """``reasons`` maps a sensor key to why it has no statistics in this window (switched off, warming up ...)."""
    reasons = reasons or {}
    rag = [w for w in windows if get_spec(w.sensor).rag]
    if not rag:
        return None
    ws, we = rag[0].window_start, rag[0].window_end
    local_start = ws.astimezone(tz)
    part = day_part(local_start)
    header = (
        f"Field sensor readings from {device_id} on {local_start:%A %d %B %Y}, "
        f"{local_start:%H:%M}–{we.astimezone(tz):%H:%M} ({part}, farm time)."
    )
    lines, notable = [header], []
    by_sensor = {w.sensor: w for w in rag}
    for key in _sensor_order(by_sensor):
        w, spec = by_sensor[key], get_spec(key)
        band = spec.band_for(w.avg_value)
        band_txt = f" — {band.label}: {band.note}" if band else ""
        lines.append(
            f"- {spec.label}: average {spec.with_unit(w.avg_value)} "
            f"(min {spec.with_unit(w.min_value)}, max {spec.with_unit(w.max_value)}, "
            f"{w.count} reading{'s' if w.count != 1 else ''}){band_txt}."
        )
        peak_band = spec.band_for(w.max_value)
        if peak_band and band and peak_band.label != band.label and peak_band is spec.bands[-1]:
            notable.append(f"{spec.label.lower()} peaked in the '{peak_band.label}' band at {spec.with_unit(w.max_value)}")
        low_band = spec.band_for(w.min_value)
        if low_band and band and low_band.label != band.label and low_band is spec.bands[0]:
            notable.append(f"{spec.label.lower()} dipped into the '{low_band.label}' band at {spec.with_unit(w.min_value)}")
    for key in _sensor_order(k for k in reasons if k not in by_sensor and get_spec(k).rag):
        lines.append(f"- {get_spec(key).label}: not recorded in this window - {reasons[key]}.")
    if "soil_moisture" not in by_sensor and "soil_moisture" not in reasons:
        lines.append("- Soil moisture: not measured in this window (probe arm raised or rover moving).")
    if notable:
        lines.append("Notable: " + "; ".join(notable) + ".")
    return ChunkDraft(
        title=f"Sensor window · {local_start:%a %d %b %Y %H:%M}–{we.astimezone(tz):%H:%M} · {device_id}",
        content="\n".join(lines),
        time_start=ws,
        time_end=we,
        meta={
            "kind": "window",
            "device_id": device_id,
            "date": local_start.date().isoformat(),
            "day_part": part,
            "sensors": sorted(by_sensor),
            "not_recorded": {k: v for k, v in reasons.items() if k not in by_sensor},
            "stats": {
                k: {"avg": round(w.avg_value, 3), "min": w.min_value, "max": w.max_value, "count": w.count}
                for k, w in by_sensor.items()
            },
        },
    )


@dataclass
class _Agg:
    count: int = 0
    total: float = 0.0
    min_value: float = float("inf")
    max_value: float = float("-inf")
    min_ts: datetime | None = None
    max_ts: datetime | None = None

    def add(self, w: SensorWindow) -> None:
        self.count += w.count
        self.total += w.avg_value * w.count
        if w.min_value < self.min_value:
            self.min_value, self.min_ts = w.min_value, w.min_ts
        if w.max_value > self.max_value:
            self.max_value, self.max_ts = w.max_value, w.max_ts

    @property
    def avg(self) -> float:
        return self.total / self.count if self.count else float("nan")


def daily_source_id(device_id: str, d: date) -> str:
    return f"{device_id}:{d.isoformat()}"


def _coverage(windows: list[SensorWindow], tz: ZoneInfo) -> str:
    """Contiguous spans of time with data, e.g. '06:00–09:15 and 16:00–18:00'."""
    spans: list[list[datetime]] = []
    for ws, we in sorted({(w.window_start, w.window_end) for w in windows}):
        if spans and ws <= spans[-1][1]:
            spans[-1][1] = max(spans[-1][1], we)
        else:
            spans.append([ws, we])
    parts = [f"{a.astimezone(tz):%H:%M}–{b.astimezone(tz):%H:%M}" for a, b in spans]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def daily_document(
    device_id: str, day: date, windows: list[SensorWindow], tz: ZoneInfo, switch_lines: list[str] | None = None
) -> ChunkDraft | None:
    """``switch_lines`` describe sensors that were switched off or silent for part of the day."""
    rag = [w for w in windows if get_spec(w.sensor).rag]
    if not rag:
        return None
    per_sensor: dict[str, _Agg] = defaultdict(_Agg)
    per_part: dict[tuple[str, str], _Agg] = defaultdict(_Agg)
    events: list[str] = []
    for w in sorted(rag, key=lambda w: w.window_start):
        per_sensor[w.sensor].add(w)
        per_part[(w.sensor, day_part(w.window_start.astimezone(tz)))].add(w)

    n_windows = len({w.window_start for w in rag})
    n_readings = sum(w.count for w in rag)
    lines = [
        f"Daily field summary for {day:%A %d %B %Y} from {device_id}. "
        f"Data covers {_coverage(rag, tz)} farm time ({n_windows} windows, {n_readings} readings)."
    ]
    if switch_lines:
        lines.append("Sensor on/off during the day: " + " ".join(switch_lines))
    parts_order = day_part_order()
    for key in _sensor_order(per_sensor):
        spec, agg = get_spec(key), per_sensor[key]
        band = spec.band_for(agg.avg)
        lines.append(
            f"{spec.label}: daily average {spec.with_unit(agg.avg)}"
            + (f" ({band.label})" if band else "")
            + f"; lowest {spec.with_unit(agg.min_value)} at {fmt_local(agg.min_ts, tz, '%H:%M')}; "
            f"highest {spec.with_unit(agg.max_value)} at {fmt_local(agg.max_ts, tz, '%H:%M')}."
        )
        profile = [
            f"{p} {spec.with_unit(per_part[(key, p)].avg)}" for p in parts_order if (key, p) in per_part
        ]
        if len(profile) > 1:
            lines.append(f"  By time of day: {', '.join(profile)}.")
        # Threshold events: first time the extreme bands were entered.
        if spec.bands:
            hi_band, lo_band = spec.bands[-1], spec.bands[0]
            first_hi = next((w for w in sorted(rag, key=lambda w: w.window_start)
                             if w.sensor == key and hi_band.contains(w.max_value)), None)
            first_lo = next((w for w in sorted(rag, key=lambda w: w.window_start)
                             if w.sensor == key and lo_band.contains(w.min_value)), None)
            if first_hi:
                events.append(
                    f"{spec.label.lower()} reached '{hi_band.label}' from {fmt_local(first_hi.window_start, tz, '%H:%M')}"
                    + (f" ({hi_band.note})" if hi_band.note else "")
                )
            if first_lo and lo_band.label not in ("baseline",):
                events.append(
                    f"{spec.label.lower()} fell into '{lo_band.label}' from {fmt_local(first_lo.window_start, tz, '%H:%M')}"
                    + (f" ({lo_band.note})" if lo_band.note else "")
                )
    if events:
        lines.append("Threshold events: " + "; ".join(events) + ".")
    start = min(w.window_start for w in rag)
    end = max(w.window_end for w in rag)
    return ChunkDraft(
        title=f"Daily sensor summary · {day:%a %d %b %Y} · {device_id}",
        content="\n".join(lines),
        time_start=start,
        time_end=end,
        meta={
            "kind": "daily",
            "device_id": device_id,
            "date": day.isoformat(),
            "sensors": sorted(per_sensor),
            "switch_notes": switch_lines or [],
            "stats": {
                k: {"avg": round(a.avg, 3), "min": a.min_value, "max": a.max_value, "count": a.count}
                for k, a in per_sensor.items()
            },
        },
    )


DIGEST_SYSTEM = """You are an agronomy assistant writing a short interpretation of one day of \
field sensor data for a farmer.
Rules:
- Use ONLY the facts in the provided summary. Do not introduce any new numbers, dates or times.
- 3 to 5 sentences in plain language.
- Mention plausible agronomic implications (irrigation need, fungal disease pressure, heat \
stress, unusual gas signals) as possibilities, not certainties.
- MQ gas values are uncalibrated relative indices; never convert them to ppm."""


def llm_digest(llm: LLM, daily: ChunkDraft, device_id: str, day: date) -> ChunkDraft:
    text = llm.complete(
        system=DIGEST_SYSTEM,
        turns=[ChatTurn("user", f"Daily summary:\n{daily.content}\n\nWrite the interpretation.")],
        temperature=0.3,
        max_tokens=600,
    ).strip()
    return ChunkDraft(
        title=f"AI interpretation of field conditions · {day:%a %d %b %Y} · {device_id}",
        content=f"AI-generated interpretation (not measurements) for {day:%A %d %B %Y}: {text}",
        time_start=daily.time_start,
        time_end=daily.time_end,
        meta={
            "kind": "digest",
            "generated_by": "llm",
            "model": llm.model_id,
            "device_id": device_id,
            "date": day.isoformat(),
            "rollup_hash": daily.content_hash,
        },
    )
