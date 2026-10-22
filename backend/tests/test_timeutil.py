from datetime import datetime, timedelta, timezone

from gaia.core.chunking import chunk_text
from gaia.core.timeutil import day_part, floor_window, resolve_time_expr

from .conftest import FIXED_NOW, IST


def test_floor_window_aligns_to_local_clock():
    ts = datetime(2025, 6, 15, 6, 7, 30, tzinfo=IST)
    start = floor_window(ts, 15, IST)
    assert start.astimezone(IST) == datetime(2025, 6, 15, 6, 0, tzinfo=IST)
    assert start.tzinfo == timezone.utc
    # Hourly windows follow local hours, not UTC hours (IST is UTC+5:30).
    assert floor_window(ts, 60, IST).astimezone(IST).minute == 0


def test_day_parts():
    assert day_part(datetime(2025, 6, 15, 6, 0, tzinfo=IST)) == "early morning"
    assert day_part(datetime(2025, 6, 15, 13, 0, tzinfo=IST)) == "afternoon"
    assert day_part(datetime(2025, 6, 15, 23, 0, tzinfo=IST)) == "late evening"


def test_resolve_days_and_parts():
    today = resolve_time_expr("day:0", FIXED_NOW, IST)
    assert today.label == "today"
    assert today.start.astimezone(IST) == datetime(2025, 6, 15, 0, 0, tzinfo=IST)
    assert today.end == FIXED_NOW  # clipped to now

    yesterday = resolve_time_expr("day:-1", FIXED_NOW, IST)
    assert yesterday.end - yesterday.start == timedelta(days=1)

    last_night = resolve_time_expr("daypart:-1:night", FIXED_NOW, IST)
    assert last_night.label == "last night"
    assert last_night.start.astimezone(IST).hour == 21
    assert last_night.end.astimezone(IST) == datetime(2025, 6, 15, 5, 0, tzinfo=IST)

    morning = resolve_time_expr("daypart:0:morning", FIXED_NOW, IST)
    assert morning.label == "this morning"
    assert morning.start.astimezone(IST).hour == 5 and morning.end.astimezone(IST).hour == 12


def test_resolve_relative_and_invalid():
    assert resolve_time_expr("last_hours:6", FIXED_NOW, IST).start == FIXED_NOW - timedelta(hours=6)
    week = resolve_time_expr("week:0", FIXED_NOW, IST)
    assert week.start.astimezone(IST).weekday() == 0
    assert resolve_time_expr("date:2025-06-10", FIXED_NOW, IST).label.startswith("Tue 10 Jun")
    for bad in ("day:3", "daypart:0:brunch", "last_hours:0", "tomorrow", "date:2025-13-01"):
        assert resolve_time_expr(bad, FIXED_NOW, IST) is None
    # Tonight has not started at 15:00, so it resolves to nothing.
    assert resolve_time_expr("daypart:0:night", FIXED_NOW, IST) is None


def test_chunk_text_splits_with_overlap():
    text = "\n\n".join(f"Paragraph {i}. " + "word " * 120 for i in range(6))
    chunks = chunk_text(text, size=800, overlap=100)
    assert len(chunks) > 3
    assert all(len(c) <= 900 for c in chunks)
    assert chunk_text("   ") == []
