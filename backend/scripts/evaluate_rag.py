"""Evaluate Pipeline A ("Query my farm") on a deterministic synthetic farm.

    python -m scripts.evaluate_rag              # configured providers (e.g. Gemini) if keys exist
    python -m scripts.evaluate_rag --offline    # heuristic planner + no LLM (retrieval only)
    python -m scripts.evaluate_rag --out report.json

What it measures, per question and overall:
  * planner accuracy   - did query understanding pick the right intent, sensors and time range?
  * numeric recall     - do the ground-truth numbers (computed independently from the RAW readings,
                         not from the window aggregates) appear in the answer?
  * grounding          - does the answer cite the expected evidence types (sensor facts, diagnoses,
                         agent findings, farmer notes)? hallucinated citation IDs are counted too.
  * latency            - end-to-end, and per stage (ingest / plan / retrieve / generate)

The run uses a throw-away SQLite database, so it never touches your real data.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sqlalchemy as sa  # noqa: E402

from gaia.config import Settings, get_settings  # noqa: E402
from gaia.container import Container  # noqa: E402
from gaia.core.timeutil import get_tz  # noqa: E402
from gaia.db.models import Diagnosis, FarmDocument, SensorReading  # noqa: E402
from gaia.ingestion.telemetry import TelemetryBatch, ingest_batch  # noqa: E402
from gaia.knowledge.documents import index_farm_document  # noqa: E402
from gaia.pipeline_b.agent import AgentFindings, AgentRun  # noqa: E402
from gaia.pipeline_b.vlm import VLMDiagnosis  # noqa: E402
from gaia.providers.embeddings import HashingEmbedder  # noqa: E402
from gaia.providers.llm import UnavailableLLM  # noqa: E402
from gaia.providers.websearch import NullSearch, WebResult  # noqa: E402
from scripts.farm_model import FarmModel  # noqa: E402

TZ = get_tz("Asia/Kolkata")
NOW = datetime(2025, 6, 15, 15, 0, tzinfo=TZ).astimezone(timezone.utc)  # evaluation clock
DAYS = [datetime(2025, 6, d, tzinfo=TZ).date() for d in (13, 14, 15)]
SESSIONS = [((6, 0), (9, 0)), ((16, 0), (18, 30))]


@dataclass
class Case:
    question: str
    intent: str
    sensors: list[str] = field(default_factory=list)
    time_ranges: list[str] = field(default_factory=list)
    numbers: list[str] = field(default_factory=list)  # filled from ground truth
    cite_types: list[str] = field(default_factory=list)  # e.g. ["sensor_stats"]; "a|b" accepts either
    keywords: list[str] = field(default_factory=list)


def seed(c: Container) -> None:
    model = FarmModel(seed=11, spike_probability=0.0)
    for i, day in enumerate(DAYS):
        for s_idx, ((h0, m0), (h1, m1)) in enumerate(SESSIONS):
            t = datetime(day.year, day.month, day.day, h0, m0, tzinfo=TZ)
            end = datetime(day.year, day.month, day.day, h1, m1, tzinfo=TZ)
            if i == 1 and s_idx == 1:
                model.force_gas_spike(t + timedelta(minutes=40))
            readings = []
            while t < end and t.astimezone(timezone.utc) < NOW:
                parked = (t.minute % 15) >= 5
                values = model.sample(t)
                if not parked:
                    values.pop("soil_moisture")
                readings.append({"ts": t.astimezone(timezone.utc).isoformat(), "values": values,
                                 "state": {"moving": not parked, "arm_deployed": parked}})
                t += timedelta(minutes=1)
            if readings:
                with c.session_factory() as s:
                    ingest_batch(s, TelemetryBatch.model_validate({"device_id": "rover-01", "readings": readings}),
                                 c.settings, received_at=NOW)
                    s.commit()
    c.ingestion.sync(now=NOW)

    with c.session_factory() as s:
        for title, text in [
            ("Biogas unit", "The biogas digester is next to the cattle shed in the east corner of the farm."),
            ("Planting log", "Tomato variety Arka Rakshak was planted in the north plot on 2 June."),
        ]:
            doc = FarmDocument(kind="note", title=title, content=text)
            s.add(doc)
            s.flush()
            index_farm_document(s, c.embedder, doc)
        # A Pipeline B result, written exactly as the diagnosis service writes it.
        d = Diagnosis(source="rover", image_path="eval.jpg", image_sha256="0" * 64, status="completed",
                      stage="done", created_at=datetime(2025, 6, 15, 7, 30, tzinfo=TZ))
        s.add(d)
        s.flush()
        analysis = VLMDiagnosis(
            is_plant_image=True, plant="Tomato", plant_part="leaf", health_status="unhealthy",
            diagnosis="Possible early blight", issue_categories=["fungal_infection", "disease"],
            evidence=["Brown lesions with concentric rings on lower leaves"], differential=["Septoria leaf spot"],
            confidence=0.74, image_quality="good",
        )
        run = AgentRun(
            findings=AgentFindings(
                headline="Likely early blight; remove infected leaves and improve airflow [W1].",
                condition_overview="Early blight is a fungal disease caused by Alternaria solani [W1].",
                recommended_actions=["Remove and destroy infected lower leaves [W1]",
                                     "Apply a labelled copper fungicide if it spreads [W1]"],
                preventive_measures=["Mulch the soil and avoid overhead irrigation [W1]"],
                cited_sources=["W1"],
            ),
            results=[WebResult("Early blight of tomato", "https://extension.umn.edu/diseases/early-blight",
                               "Management of early blight", "tomato early blight")],
            cited=["W1"], web_grounded=True,
        )
        c.diagnosis._write_knowledge(s, d, analysis, run)
        s.commit()


def truth(c: Container, sensor: str, start: datetime, end: datetime) -> dict:
    """Ground truth straight from raw readings (independent of the window aggregates)."""
    with c.session_factory() as s:
        vals = s.scalars(
            sa.select(SensorReading.value).where(
                SensorReading.sensor == sensor, SensorReading.ts >= start, SensorReading.ts < end
            )
        ).all()
    return {"avg": statistics.fmean(vals), "min": min(vals), "max": max(vals), "n": len(vals)}


def day(d, h0=0, h1=24):
    base = datetime(d.year, d.month, d.day, tzinfo=TZ)
    return base + timedelta(hours=h0), min(base + timedelta(hours=h1), NOW)


def build_cases(c: Container) -> list[Case]:
    today, yesterday = DAYS[2], DAYS[1]
    soil_today = truth(c, "soil_moisture", *day(today))
    soil_yday = truth(c, "soil_moisture", *day(yesterday))
    hum_morning = truth(c, "humidity", *day(today, 5, 12))
    temp_yday = truth(c, "temperature", *day(yesterday))
    mq5_3d = truth(c, "mq5", day(DAYS[0])[0], NOW)
    return [
        Case("What was the average soil moisture today?", "sensor_statistics", ["soil_moisture"], ["day:0"],
             [f"{soil_today['avg']:.1f}"], ["sensor_stats"]),
        Case("How humid was my field this morning?", "sensor_statistics", ["humidity"], ["daypart:0:morning"],
             [f"{hum_morning['avg']:.0f}"], ["sensor_stats"]),
        Case("Was the soil moisture lower than yesterday?", "comparison", ["soil_moisture"], ["day:0", "day:-1"],
             [f"{soil_today['avg']:.1f}", f"{soil_yday['avg']:.1f}"], ["sensor_comparison"]),
        Case("What was the highest temperature yesterday?", "sensor_statistics", ["temperature"], ["day:-1"],
             [f"{temp_yday['max']:.1f}"], ["sensor_stats"]),
        Case("Any unusual gas readings in the last 3 days?", "sensor_statistics", ["mq2", "mq5", "mq6"],
             ["last_days:3"], [f"{mq5_3d['max']:.0f}"], ["sensor_stats|sensor_excursion"]),
        Case("What crop problems have been identified in my field?", "diagnosis_history", [], [],
             [], ["diagnosis"], ["blight"]),
        Case("What treatment did the system find for the tomato problem?", "diagnosis_history", [], [],
             [], ["agent_finding"], ["leaves"]),
        Case("What environmental conditions were recorded when this issue was detected?", "diagnosis_history",
             [], [], [], ["sensor_linked"], []),
        Case("Where is the biogas unit?", "farm_knowledge", [], [], [], ["farm_note"], ["cattle shed"]),
        Case("How did the temperature change through the day today?", "trend", ["temperature"], ["day:0"],
             [], ["sensor_profile|sensor_daily"]),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="no LLM: heuristic planner + extractive answers")
    parser.add_argument("--out", type=Path, help="write a JSON report here")
    parser.add_argument("--keep", action="store_true", help="keep the temporary evaluation database")
    args = parser.parse_args()

    base = get_settings()
    tmp = Path(tempfile.mkdtemp(prefix="gaia-eval-"))
    settings = Settings(
        **{**base.model_dump(), "database_url": f"sqlite:///{(tmp / 'eval.db').as_posix()}", "data_dir": tmp,
           "scheduler_enabled": False, "farm_timezone": "Asia/Kolkata", "window_minutes": 15,
           "llm_digest_enabled": False}
    )
    overrides = {"search": NullSearch()}
    if args.offline:
        overrides.update(llm=UnavailableLLM("offline evaluation"), embedder=HashingEmbedder(settings.embedding_dim))
    c = Container.build(settings, **overrides)
    print(f"LLM: {c.llm.model_id} | embedder: {c.embedder.model_id} | db: {tmp / 'eval.db'}")
    print("Seeding the synthetic farm...")
    seed(c)
    cases = build_cases(c)

    rows = []
    for case in cases:
        t0 = time.perf_counter()
        with c.session_factory() as s:
            ans = c.chat.farm.answer(s, case.question, [], now=NOW)
        latency = int((time.perf_counter() - t0) * 1000)
        plan = ans.plan
        planner_ok = (
            plan["intent"] == case.intent
            and (not case.sensors or sorted(plan["sensors"]) == sorted(case.sensors))
            and (not case.time_ranges or plan["time_ranges"] == case.time_ranges)
        )
        text = ans.answer
        found = [n for n in case.numbers if re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", text)]
        cited_types = {src.source_type for src in ans.sources}
        cites_ok = all(any(alt in cited_types for alt in t.split("|")) for t in case.cite_types)
        kw_ok = all(k.lower() in text.lower() for k in case.keywords)
        rows.append({
            "question": case.question,
            "planner": plan["planner"],
            "planner_ok": planner_ok,
            "numbers_expected": case.numbers,
            "numbers_found": found,
            "cited_types": sorted(t for t in cited_types if t),
            "citations_ok": cites_ok,
            "keywords_ok": kw_ok,
            "grounding": ans.grounding,
            "llm_used": ans.llm_used,
            "latency_ms": latency,
            "timings_ms": ans.timings_ms,
            "answer": text,
        })
        mark = "PASS" if planner_ok and cites_ok and kw_ok and len(found) == len(case.numbers) else "FAIL"
        print(f"[{mark}] {case.question}\n       plan={plan['intent']} {plan['sensors']} {plan['time_ranges']} "
              f"| numbers {len(found)}/{len(case.numbers)} | cites {sorted(cited_types)} | {latency} ms")

    n = len(rows)
    expected_numbers = sum(len(r["numbers_expected"]) for r in rows)
    summary = {
        "cases": n,
        "planner_accuracy": sum(r["planner_ok"] for r in rows) / n,
        "numeric_recall": (sum(len(r["numbers_found"]) for r in rows) / expected_numbers) if expected_numbers else None,
        "citation_accuracy": sum(r["citations_ok"] for r in rows) / n,
        "keyword_accuracy": sum(r["keywords_ok"] for r in rows) / n,
        "answers_with_citations": sum(bool(r["cited_types"]) for r in rows) / n,
        "median_latency_ms": statistics.median(r["latency_ms"] for r in rows),
        "llm": c.llm.model_id,
        "embedder": c.embedder.model_id,
    }
    print("\nSummary:")
    for k, v in summary.items():
        print(f"  {k:24s} {v:.2%}" if isinstance(v, float) and k != "median_latency_ms" else f"  {k:24s} {v}")
    if args.out:
        args.out.write_text(json.dumps({"summary": summary, "cases": rows}, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"\nReport written to {args.out}")
    c.engine.dispose()
    if not args.keep:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
