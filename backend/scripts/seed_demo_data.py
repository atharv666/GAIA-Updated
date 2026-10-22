"""Backfill a few days of realistic rover sessions so the chatbot has data to talk about.

    python -m scripts.seed_demo_data --days 3
    python -m scripts.seed_demo_data --days 5 --notes

Readings go through the same validation/ingestion path as real telemetry, then
windows are aggregated, documented and embedded into the knowledge store.
Rover sessions run 06:00-09:00 and 16:00-18:30 farm time; the soil probe is
only lowered while parked, exactly like the real rover.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gaia.config import get_settings  # noqa: E402
from gaia.container import Container  # noqa: E402
from gaia.core.timeutil import get_tz  # noqa: E402
from gaia.db.base import utcnow  # noqa: E402
from gaia.ingestion.telemetry import TelemetryBatch, ingest_batch  # noqa: E402
from scripts.farm_model import FarmModel  # noqa: E402

SESSIONS = [(time(6, 0), time(9, 0)), (time(16, 0), time(18, 30))]
NOTES = [
    ("Planting log", "Planted tomato (variety Arka Rakshak) in the north plot on 2 September. Chilli seedlings "
                     "transplanted to the south beds on 10 September."),
    ("Irrigation schedule", "Drip irrigation runs 30 minutes every morning at 6:30 in the north plot. The south "
                            "beds are watered every second evening."),
    ("Biogas unit", "The biogas digester is next to the cattle shed in the east corner; the rover passes it on "
                    "the evening route, so MQ5 (methane) readings there can be higher."),
]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=3, help="days of history to generate, ending now")
    parser.add_argument("--device-id", default="rover-01")
    parser.add_argument("--step", type=int, default=60, help="seconds between samples")
    parser.add_argument("--notes", action="store_true", help="also add example farmer notes")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    settings = get_settings()
    settings.scheduler_enabled = False
    container = Container.build(settings)
    tz = get_tz(settings.farm_timezone)
    model = FarmModel(seed=args.seed, spike_probability=0.0)
    now = utcnow()
    first_day = now.astimezone(tz).date() - timedelta(days=args.days - 1)

    total = 0
    for d in range(args.days):
        day = first_day + timedelta(days=d)
        for s_idx, (start_t, end_t) in enumerate(SESSIONS):
            t = datetime.combine(day, start_t, tzinfo=tz)
            end = datetime.combine(day, end_t, tzinfo=tz)
            if d == 1 and s_idx == 1:
                model.force_gas_spike(t + timedelta(minutes=45))  # one evening methane event near the biogas unit
            readings = []
            while t < end and t.astimezone(timezone.utc) < now:
                # Drive 5 minutes, park 10 minutes: soil readings only while parked with the probe down.
                parked = (t.minute % 15) >= 5
                values = model.sample(t)
                if not parked:
                    values.pop("soil_moisture")
                readings.append({
                    "ts": t.astimezone(timezone.utc).isoformat(),
                    "values": values,
                    "state": {"moving": not parked, "arm_deployed": parked},
                })
                t += timedelta(seconds=args.step)
            if not readings:
                continue
            with container.session_factory() as s:
                result = ingest_batch(
                    s, TelemetryBatch.model_validate({"device_id": args.device_id, "readings": readings}), settings
                )
                s.commit()
            total += result.accepted
            print(f"{day} session {s_idx + 1}: {result.accepted} values stored ({result.duplicates} duplicates)")

    if args.notes:
        from gaia.db.models import FarmDocument
        from gaia.knowledge.documents import index_farm_document

        with container.session_factory() as s:
            for title, text in NOTES:
                doc = FarmDocument(kind="note", title=title, content=text)
                s.add(doc)
                s.flush()
                index_farm_document(s, container.embedder, doc)
            s.commit()
        print(f"Added {len(NOTES)} farmer notes")

    print("Aggregating and indexing windows (this embeds each window document)...")
    report = container.ingestion.sync(include_open=True)
    print(f"Done: {total} values, {report.windows_indexed} windows indexed, "
          f"{report.days_rolled_up} daily summaries, errors={report.errors or 'none'}")


if __name__ == "__main__":
    main()
