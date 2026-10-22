"""Farm-local time handling: window bucketing, day parts and time-expression resolution.

The chat query planner (LLM or heuristic) never computes dates itself. It emits
tokens from a small vocabulary (``day:-1``, ``daypart:0:morning`` ...) and this
module resolves them deterministically against the farm clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

UTC = timezone.utc


@lru_cache
def get_tz(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def floor_window(ts: datetime, minutes: int, tz: ZoneInfo) -> datetime:
    """Start (UTC) of the fixed local-clock window containing ``ts``."""
    local = ts.astimezone(tz)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    offset = int((local - midnight).total_seconds() // 60)
    floored = midnight + timedelta(minutes=offset - offset % minutes)
    return floored.astimezone(UTC)


# Non-overlapping parts used for breakdowns and document text.
_DAY_PART_BREAKDOWN = (
    (0, 5, "night"),
    (5, 8, "early morning"),
    (8, 12, "morning"),
    (12, 17, "afternoon"),
    (17, 21, "evening"),
    (21, 24, "late evening"),
)

# Ranges used when a farmer names a part of the day ("this morning").
# Hours may exceed 24 to wrap into the next day.
DAY_PARTS = {
    "early_morning": (5, 8),
    "morning": (5, 12),
    "afternoon": (12, 17),
    "evening": (17, 21),
    "night": (21, 29),
}


def day_part(local_dt: datetime) -> str:
    h = local_dt.hour
    return next(label for lo, hi, label in _DAY_PART_BREAKDOWN if lo <= h < hi)


def day_part_order() -> list[str]:
    return [label for _, _, label in _DAY_PART_BREAKDOWN]


@dataclass(frozen=True)
class TimeRange:
    expr: str
    label: str
    start: datetime  # UTC, inclusive
    end: datetime  # UTC, exclusive

    def describe(self, tz: ZoneInfo) -> str:
        s, e = self.start.astimezone(tz), self.end.astimezone(tz)
        ends_at_midnight = e.time() == time(0) and e.date() - s.date() == timedelta(days=1)
        if s.date() == e.date() or ends_at_midnight:
            end_txt = "24:00" if ends_at_midnight else f"{e:%H:%M}"
            return f"{s:%a %d %b %Y}, {s:%H:%M}–{end_txt}"
        return f"{s:%a %d %b %H:%M} – {e:%a %d %b %H:%M}"

    def to_dict(self, tz: ZoneInfo) -> dict:
        return {
            "expr": self.expr,
            "label": self.label,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "description": self.describe(tz),
        }


def _day_label(offset: int, d: date) -> str:
    if offset == 0:
        return "today"
    if offset == -1:
        return "yesterday"
    return f"{d:%a %d %b}"


_PART_LABELS = {
    "early_morning": "early morning",
    "morning": "morning",
    "afternoon": "afternoon",
    "evening": "evening",
    "night": "night",
}


def _part_label(offset: int, part: str, d: date) -> str:
    name = _PART_LABELS[part]
    if offset == 0:
        return "tonight" if part == "night" else f"this {name}"
    if offset == -1:
        return "last night" if part == "night" else f"yesterday {name}"
    return f"{d:%a %d %b} {name}"


TIME_EXPR_HELP = """\
now                 -> the last 45 minutes (current conditions)
day:N               -> a whole calendar day, N=0 today, -1 yesterday, -2 ...
daypart:N:PART      -> part of day N; PART in early_morning|morning|afternoon|evening|night
                       (night = 21:00 to 05:00 next day, so "last night" is daypart:-1:night)
last_hours:H        -> the last H hours (1-720)
last_days:D         -> today plus the previous D-1 days (1-90)
week:N              -> calendar week starting Monday, N=0 this week, -1 last week
date:YYYY-MM-DD     -> a specific calendar day
all                 -> the entire sensor history"""


def resolve_time_expr(expr: str, now: datetime, tz: ZoneInfo) -> TimeRange | None:
    """Turn a planner token into an absolute UTC range. Returns None when invalid."""
    raw = expr.strip().lower()
    local_now = now.astimezone(tz)
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)

    def build(label: str, start_local: datetime, end_local: datetime) -> TimeRange | None:
        start, end = start_local.astimezone(UTC), min(end_local.astimezone(UTC), now)
        if start >= end:
            return None
        return TimeRange(raw, label, start, end)

    try:
        if raw in ("now", "latest", "current"):
            return build("right now", local_now - timedelta(minutes=45), local_now + timedelta(seconds=1))
        if raw == "all":
            return TimeRange(raw, "all recorded history", datetime(2000, 1, 1, tzinfo=UTC), now)
        if m := re.fullmatch(r"day:(-?\d+)", raw):
            n = int(m.group(1))
            if n > 0 or n < -3650:
                return None
            start = today + timedelta(days=n)
            return build(_day_label(n, start.date()), start, start + timedelta(days=1))
        if m := re.fullmatch(r"daypart:(-?\d+):([a-z_]+)", raw):
            n, part = int(m.group(1)), m.group(2)
            if n > 0 or part not in DAY_PARTS:
                return None
            lo, hi = DAY_PARTS[part]
            base = today + timedelta(days=n)
            return build(_part_label(n, part, base.date()), base + timedelta(hours=lo), base + timedelta(hours=hi))
        if m := re.fullmatch(r"last_hours:(\d+)", raw):
            h = int(m.group(1))
            if not 1 <= h <= 720:
                return None
            return build(f"the last {h} hour{'s' if h != 1 else ''}", local_now - timedelta(hours=h), local_now)
        if m := re.fullmatch(r"last_days:(\d+)", raw):
            d = int(m.group(1))
            if not 1 <= d <= 90:
                return None
            label = "today" if d == 1 else f"the last {d} days"
            return build(label, today - timedelta(days=d - 1), local_now)
        if m := re.fullmatch(r"week:(-?\d+)", raw):
            n = int(m.group(1))
            if n > 0:
                return None
            monday = today - timedelta(days=today.weekday()) + timedelta(weeks=n)
            label = "this week" if n == 0 else "last week" if n == -1 else f"week of {monday:%d %b}"
            return build(label, monday, monday + timedelta(days=7))
        if m := re.fullmatch(r"date:(\d{4}-\d{2}-\d{2})", raw):
            d = date.fromisoformat(m.group(1))
            start = datetime.combine(d, time(0), tzinfo=tz)
            return build(f"{d:%a %d %b %Y}", start, start + timedelta(days=1))
    except (ValueError, OverflowError):
        return None
    return None


def local_date(ts: datetime, tz: ZoneInfo) -> date:
    return ts.astimezone(tz).date()


def day_bounds(d: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime.combine(d, time(0), tzinfo=tz)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def fmt_local(ts: datetime, tz: ZoneInfo, pattern: str = "%a %d %b %H:%M") -> str:
    return ts.astimezone(tz).strftime(pattern)
