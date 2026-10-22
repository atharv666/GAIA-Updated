"""Load demo data into a RUNNING GAIA backend through its public API.

Everything goes through the same endpoints the real system uses, so running this is also a demonstration of
how data gets in and where it ends up:

  1. sensor telemetry  -> POST /api/v1/telemetry          (same JSON the ESP32 posts, X-Device-Key header)
  2. farmer notes      -> POST /api/v1/knowledge/notes    (embedded into the knowledge store)
  3. farm documents    -> POST /api/v1/knowledge/documents
  4. leaf photos       -> POST /api/v1/diagnoses          (Pipeline B: VLM + search agent; needs the Gemini key)
  5. window summaries  -> POST /api/v1/ingest/sync        (aggregate + embed the sensor windows)

Start the backend first (with GEMINI_API_KEY in backend/.env so embeddings and diagnoses use Gemini), then:

    cd backend
    .venv\\Scripts\\python ..\\demo_data\\load_demo_data.py --backend http://localhost:8000

Safe to re-run: notes, documents and photos that already exist are skipped. Each run adds fresh sensor
history ending "now", so questions like "today" and "yesterday" always have data.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "backend"))
from scripts.farm_model import FarmModel  # noqa: E402  (synthetic but plausible field conditions)

SESSIONS = [(dtime(6, 0), dtime(9, 0)), (dtime(16, 0), dtime(18, 30))]  # rover runs, farm time
VIDEO_ONLY = (dtime(9, 0), dtime(9, 20))  # last day only: camera on, every sensor switched off
ALL_ON = {"dht11": True, "soil_moisture": True, "mq5": True, "mq6": True}
SWITCH_VALUES = {"dht11": ("temperature", "humidity"), "soil_moisture": ("soil_moisture",),
                 "mq5": ("mq5",), "mq6": ("mq6",)}


def session_switches(day_index: int, session_index: int, days: int) -> dict[str, bool]:
    """Which sensors the farmer had switched on: the demo includes a few power-saving choices on purpose."""
    last = days - 1
    if day_index == last and session_index == 0:
        return {**ALL_ON, "mq5": False, "mq6": False}  # gas sensors off this morning
    if day_index == last - 1 and session_index == 1 and days > 2:
        return {**ALL_ON, "soil_moisture": False}  # soil probe off yesterday evening
    return dict(ALL_ON)
API = "/api/v1"

QUESTIONS = """
Suggested demo questions
  Query my farm
    - What is the current temperature and humidity?
    - How humid was my field this morning?
    - Was the soil moisture lower today than yesterday?
    - Were there any unusual gas readings in the last few days?   (a methane spike was planted on day 2 evening)
    - What were the gas readings this morning?           (the gas sensors were switched off - the AI explains why)
    - How was the soil moisture yesterday evening?       (the soil probe was switched off)
    - When was the rover collecting sensor data today?   (the sensor on/off log)
    - What crop problems have been found in my field?
    - What conditions were recorded when the early blight was detected?
    - Where is the biogas unit, and why are MQ5 readings higher there?
    - What is my irrigation schedule for the north plot?
  Open-ended
    - How do I prevent early blight in tomatoes?
    - What is the difference between early blight and late blight?
  Diagnose tab
    - Upload any file from demo_data/plant_photos/
"""


def _rover_values(model: FarmModel, local: datetime, switches: dict[str, bool] | None = None) -> dict[str, float]:
    """One sample for the rover's fitted sensors (DHT11, soil probe, MQ5, MQ6; no MQ2).

    A switched-off sensor produces nothing, exactly like the firmware.
    """
    values = model.sample(local)
    values.pop("mq2", None)
    for toggle, on in (switches or {}).items():
        if not on:
            for key in SWITCH_VALUES[toggle]:
                values.pop(key, None)
    return values


class Loader:
    def __init__(self, client: httpx.Client, *, device_key: str, api_key: str = "", log=print):
        self.c = client
        self.device_key = device_key
        self.api_headers = {"X-API-Key": api_key} if api_key else {}
        self.log = log

    # ------------------------------------------------------------------ helpers

    def _get(self, path: str, **kw):
        r = self.c.get(API + path, headers=self.api_headers, **kw)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, *, headers: dict | None = None, **kw):
        r = self.c.post(API + path, headers={**self.api_headers, **(headers or {})}, **kw)
        if r.status_code >= 400:
            detail = r.text[:300]
            raise RuntimeError(f"POST {path} -> HTTP {r.status_code}: {detail}")
        return r.json()

    # ------------------------------------------------------------------ 1. telemetry

    def load_telemetry(self, days: int, device_id: str, step_s: int, tz: ZoneInfo, seed: int) -> int:
        model = FarmModel(seed=seed, spike_probability=0.0)
        now = datetime.now(timezone.utc)
        first_day = now.astimezone(tz).date() - timedelta(days=days - 1)
        stored = 0
        for d in range(days):
            day = first_day + timedelta(days=d)
            for s_idx, (start_t, end_t) in enumerate(SESSIONS):
                t = datetime.combine(day, start_t, tzinfo=tz)
                end = datetime.combine(day, end_t, tzinfo=tz)
                switches = session_switches(d, s_idx, days)
                if d == 1 and s_idx == 1:
                    model.force_gas_spike(t + timedelta(minutes=45))  # evening methane event by the biogas unit
                readings = []
                while t < end and t.astimezone(timezone.utc) < now:
                    parked = (t.minute % 15) >= 5  # drive 5 min, park 10 min; soil only while parked
                    values = _rover_values(model, t, switches)
                    soil_ok = parked and switches["soil_moisture"]
                    if not soil_ok:
                        values.pop("soil_moisture", None)
                    readings.append({
                        "ts": t.astimezone(timezone.utc).isoformat(),
                        "values": values,
                        "state": {"moving": not parked, "arm_deployed": parked, "soil_valid": soil_ok,
                                  "sensor_switches": switches},
                    })
                    t += timedelta(seconds=step_s)
                if readings:
                    stored += self._send_batch({"device_id": device_id, "readings": readings,
                                                "sensor_switches": switches})
                if d == days - 1 and s_idx == 0:
                    stored += self._video_only(device_id, day, tz, now)
        stored += self._send_live_batch(device_id, model, tz)
        return stored

    def _video_only(self, device_id: str, day, tz: ZoneInfo, now: datetime) -> int:
        """Camera on, every sensor off: the rover still reports its own channels (distance, battery)."""
        t = datetime.combine(day, VIDEO_ONLY[0], tzinfo=tz)
        end = datetime.combine(day, VIDEO_ONLY[1], tzinfo=tz)
        off = {k: False for k in ALL_ON}
        readings = []
        while t < end and t.astimezone(timezone.utc) < now:
            readings.append({
                "ts": t.astimezone(timezone.utc).isoformat(),
                "values": {"distance_cm": 120, "battery_v": 7.9},
                "state": {"moving": False, "arm_deployed": False, "soil_valid": False, "sensor_switches": off},
            })
            t += timedelta(seconds=60)
        return self._send_batch({"device_id": device_id, "readings": readings, "sensor_switches": off}) if readings else 0

    def _send_batch(self, payload: dict) -> int:
        out = self._post("/telemetry", json=payload, headers={"X-Device-Key": self.device_key})
        self.log(f"  telemetry: {out['accepted']} values accepted, {out['duplicates']} duplicates, "
                 f"{out['rejected']} rejected ({len(payload['readings'])} readings)")
        return out["accepted"]

    def _send_live_batch(self, device_id: str, model: FarmModel, tz: ZoneInfo) -> int:
        """Last batch in the exact form the ESP32 sends without NTP: no clock, only uptime_ms."""
        base_uptime = 600_000
        readings = []
        now_local = datetime.now(tz)
        for i in range(6):
            values = _rover_values(model, now_local + timedelta(seconds=5 * i))
            readings.append({
                "uptime_ms": base_uptime + i * 5000,
                "values": values,
                "state": {"moving": False, "arm_deployed": True, "soil_valid": True, "sensor_switches": ALL_ON},
            })
        self.log("  live batch (uptime_ms style, stamped from the moment of receipt):")
        return self._send_batch({"device_id": device_id, "sent_uptime_ms": base_uptime + 5 * 5000 + 400,
                                 "readings": readings, "sensor_switches": ALL_ON})

    # ------------------------------------------------------------------ 2/3. notes + documents

    def _existing_titles(self) -> set[str]:
        return {d["title"] for d in self._get("/knowledge/documents")}

    @staticmethod
    def _fill_dates(text: str, tz: ZoneInfo) -> str:
        def repl(m: re.Match) -> str:
            day = datetime.now(tz) + timedelta(days=int(m.group(1)))
            return f"{day.day} {day:%B}"
        return re.sub(r"\{\{date:(-?\d+)\}\}", repl, text)

    def load_notes(self, tz: ZoneInfo) -> int:
        existing, added = self._existing_titles(), 0
        for path in sorted((HERE / "notes").glob("*.md")):
            lines = path.read_text(encoding="utf-8").strip().splitlines()
            title = lines[0].lstrip("# ").strip()
            body = self._fill_dates("\n".join(lines[1:]).strip(), tz)
            if title in existing:
                self.log(f"  note exists, skipped: {title}")
                continue
            out = self._post("/knowledge/notes", json={"title": title, "text": body})
            self.log(f"  note: {title} -> {len(out['chunk_ids'])} chunk(s) embedded")
            added += 1
        return added

    def load_documents(self) -> int:
        existing, added = self._existing_titles(), 0
        for path in sorted((HERE / "documents").iterdir()):
            title = path.stem.replace("_", " ").capitalize()
            if title in existing:
                self.log(f"  document exists, skipped: {title}")
                continue
            out = self._post("/knowledge/documents", files={"file": (path.name, path.read_bytes())},
                             data={"title": title})
            self.log(f"  document: {path.name} -> {out['characters']} characters, {len(out['chunk_ids'])} chunks embedded")
            added += 1
        return added

    # ------------------------------------------------------------------ 4. diagnoses

    def load_diagnoses(self, timeout_s: int, pause_s: float) -> int:
        done_notes = {d.get("note") for d in self._get("/diagnoses", params={"limit": 100})}
        photos = sorted((HERE / "plant_photos").glob("*.jpg"))
        completed = 0
        for photo in photos:
            note = f"demo: {photo.name}"
            if note in done_notes:
                self.log(f"  diagnosis exists, skipped: {photo.name}")
                continue
            job = self._post("/diagnoses", files={"image": (photo.name, photo.read_bytes(), "image/jpeg")},
                             data={"source": "rover", "note": note}, timeout=120)
            self.log(f"  diagnosing {photo.name} (job {job['id'][:8]}) ...")
            result = self._wait(job["id"], timeout_s)
            a = result.get("analysis") or {}
            if result["status"] == "completed":
                completed += 1
                self.log(f"    -> {a.get('plant')}: {a.get('health_status')} | {a.get('diagnosis')} "
                         f"(self-reported confidence {a.get('confidence')}) | "
                         f"{len(result.get('sources') or [])} web sources, web_grounded={result.get('web_grounded')}")
            else:
                self.log(f"    -> {result['status']}: {result.get('error') or 'no details'}")
            time.sleep(pause_s)  # stay under free-tier requests-per-minute limits
        return completed

    def _wait(self, diagnosis_id: str, timeout_s: int) -> dict:
        deadline = time.monotonic() + timeout_s
        stage = ""
        while True:
            d = self._get(f"/diagnoses/{diagnosis_id}")
            if d["stage"] != stage:
                stage = d["stage"]
                self.log(f"      stage: {stage}")
            if d["status"] != "processing":
                return d
            if time.monotonic() > deadline:
                return {**d, "status": "timeout", "error": f"still running after {timeout_s}s"}
            time.sleep(2)

    # ------------------------------------------------------------------ 5. index

    def sync_and_report(self, reindex: bool) -> dict:
        sync = self._post("/ingest/sync", json={"include_open": True})
        self.log(f"  sync: {sync['windows_aggregated']} windows aggregated, {sync['windows_indexed']} indexed, "
                 f"{sync['days_rolled_up']} daily summaries, errors={sync.get('errors') or 'none'}")
        if reindex:
            out = self._post("/ingest/reindex")
            self.log(f"  reindexed {out['reindexed']} chunks with {out['embedder']}")
        status = self._get("/ingest/status")
        self.log(f"  knowledge store now holds: {status['knowledge_by_type']}")
        return status


def run(client: httpx.Client, args: argparse.Namespace, log=print) -> dict:
    loader = Loader(client, device_key=args.device_key, api_key=args.api_key, log=log)
    health = client.get(f"{API}/health").json()
    tz = ZoneInfo(health.get("timezone") or "Asia/Kolkata")
    log(f"Backend: db={health['database']} llm={health['llm']['model']} vlm={health['vlm']['model']} "
        f"embedder={health['embedder']} search={health['search']['provider']}")
    if not health["llm"]["available"]:
        log("  WARNING: no LLM configured. Data loads, but chat answers will just list retrieved facts.")
    if health["embedder"].startswith("hashing"):
        log("  NOTE: offline hashing embedder in use (keyword-like). Add GEMINI_API_KEY to backend/.env, restart the "
            "backend and re-run with --reindex for semantic embeddings.")

    log("\n[1/5] Sensor telemetry (with some sensors switched off on purpose, to show how the AI explains gaps)")
    stored = loader.load_telemetry(args.days, args.device_id, args.step, tz, args.seed)
    log("\n[2/5] Farmer notes")
    notes = loader.load_notes(tz)
    log("\n[3/5] Farm documents")
    docs = loader.load_documents()
    log("\n[4/5] Crop photo diagnoses (Pipeline B)")
    diagnosed = 0
    if args.skip_diagnoses:
        log("  skipped (--skip-diagnoses)")
    elif not health["vlm"]["available"]:
        log("  skipped: no vision-language model configured (set GEMINI_API_KEY and restart the backend)")
    else:
        diagnosed = loader.load_diagnoses(args.diagnosis_timeout, args.pause)
    log("\n[5/5] Aggregate + embed sensor windows")
    status = loader.sync_and_report(args.reindex)
    log(QUESTIONS)
    return {"values": stored, "notes": notes, "documents": docs, "diagnoses": diagnosed, "status": status}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", default="http://localhost:8000", help="backend base URL")
    p.add_argument("--device-key", default="gaia-rover-dev-key", help="DEVICE_KEY from backend/.env")
    p.add_argument("--api-key", default="", help="API_KEY from backend/.env, only if you set one")
    p.add_argument("--days", type=int, default=4, help="days of sensor history ending now")
    p.add_argument("--device-id", default="rover-01")
    p.add_argument("--step", type=int, default=60, help="seconds between generated samples")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--skip-diagnoses", action="store_true", help="do not run the VLM on the sample photos")
    p.add_argument("--diagnosis-timeout", type=int, default=300, help="seconds to wait per diagnosis")
    p.add_argument("--pause", type=float, default=8.0, help="seconds between diagnoses (free-tier rate limits)")
    p.add_argument("--reindex", action="store_true", help="re-embed everything with the current embedding model")
    args = p.parse_args()
    try:
        with httpx.Client(base_url=args.backend.rstrip("/"), timeout=60) as client:
            run(client, args)
    except httpx.ConnectError:
        sys.exit(f"Cannot reach the backend at {args.backend}. Start it first (uvicorn gaia.main:app --port 8000).")
    except (RuntimeError, httpx.HTTPStatusError) as exc:
        sys.exit(f"Failed: {exc}")


if __name__ == "__main__":
    main()
