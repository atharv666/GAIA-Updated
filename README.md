# GAIA — rover-assisted Generative AI for proactive crop health

GAIA helps a farmer **ask questions about their own field** and **diagnose crops from photos**.
A small 6WD rover collects field data; two independent Generative AI pipelines turn it into answers:

* **Pipeline A — sensor-grounded assistant.** "Query my farm" answers from the farmer's own sensor log,
  earlier diagnoses and notes (RAG with exact SQL statistics + pgvector hybrid retrieval, cited answers).
  "Open-ended" answers anything else with web search and cited sources.
* **Pipeline B — vision-language crop diagnosis.** One VLM call returns structured JSON (plant, health,
  possible diagnosis, visual evidence, confidence); one research agent searches the web for care or
  treatment information; findings are shown with sources and written to the shared knowledge store.

The rover is the data-acquisition layer; the GenAI pipelines are the intelligence. Every sensor can be
switched on or off from the app to save battery, and the assistant explains the resulting gaps ("the gas
sensors were switched off 06:00-09:20") instead of guessing. Full design:
[ARCHITECTURE.md](ARCHITECTURE.md).

```text
frontend/            Flutter mobile app (Android / web)
backend/             FastAPI backend: both pipelines, knowledge store, website, simulator, evaluation
report/              capstone project report in LaTeX (Overleaf-ready) with a PDF preview
demo_data/           sample sensor history, notes, documents, leaf photos + loader (demo without the rover)
firmware/            ESP32-CAM + Arduino Nano rover firmware (compile-verified)
docs/                screenshots and evaluation report
docker-compose.yml   backend + PostgreSQL/pgvector
```

## Quick start (Windows, no Docker)

```powershell
cd backend
python -m venv .venv
.venv\Scripts\pip install -r requirements-local.txt
copy .env.example .env          # add GEMINI_API_KEY (free at https://aistudio.google.com/apikey)
.venv\Scripts\python -m scripts.seed_demo_data --days 3 --notes     # optional demo data
.venv\Scripts\uvicorn gaia.main:app --host 0.0.0.0 --port 8000
```

* Website: <http://localhost:8000> — Overview, Rover, Diagnose, Gaia AI (chat), Sensors, Knowledge.
* API docs: <http://localhost:8000/docs>
* `DATABASE_URL=embedded` (the default) starts a local PostgreSQL 16 + pgvector automatically.

**No rover yet?** Run the simulator in a second terminal; it speaks the exact firmware API (live video,
drive with failsafe, soil-probe arm, telemetry push):

```powershell
cd backend
.venv\Scripts\python -m scripts.rover_simulator --port 8090 --backend http://localhost:8000
```

and set `ROVER_BASE_URL=http://localhost:8090` and `ROVER_STREAM_URL=http://localhost:8090/stream` in
`backend/.env` (restart the backend). In the app, open Settings and use the same URLs (with your PC's LAN
IP instead of `localhost` on a phone).

**With Docker:** `docker compose up --build` (uses `pgvector/pgvector:pg16`; put keys in `backend/.env`).

### Mobile app

```bash
cd frontend
flutter pub get
flutter run                     # Android device/emulator
flutter build apk --release     # frontend/build/app/outputs/flutter-apk/app-release.apk
```

If a rebuild fails with "property 'outputFiles' is not writable because … ancestor '…' is not a directory",
the project path contains spaces, which Flutter's Gradle task mis-parses on incremental builds. Delete `build/app/intermediates/flutter/release`
(or run `flutter clean`) and build again, or keep the project in a path without spaces.

Open **Settings** (tune icon, top right) and set:

| Setting | Example |
|---|---|
| GAIA server URL | `http://192.168.1.10:8000` (the PC running the backend; emulator: `http://10.0.2.2:8000`) |
| Rover URL | `http://192.168.4.1` (rover hotspot) or the IP the rover prints when it joins your Wi-Fi |
| Relay rover telemetry | on only when the phone is on the rover's hotspot and the rover cannot reach the server |

### Rover

See [firmware/README.md](firmware/README.md) for wiring, power, flashing and calibration, and
[firmware/PROTOCOL.md](firmware/PROTOCOL.md) for the HTTP API and the ESP32 ↔ Nano protocol.

Bring-up tools (run from `backend/`; each check prints PASS / WARN / FAIL with a wiring hint):

```powershell
.venv\Scripts\python -m scripts.nano_console --list                   # find the Nano's COM port
.venv\Scripts\python -m scripts.nano_console COM5 --check --arm       # Nano alone on USB: link, sensors, soil arm
.venv\Scripts\python -m scripts.nano_console COM5 --check --motors    # + drive pulses and failsafe (wheels up!)
.venv\Scripts\python -m scripts.nano_console COM5                     # live telemetry + manual commands
.venv\Scripts\python -m scripts.rover_check http://192.168.4.1 --arm --drive   # assembled rover over Wi-Fi
```

## Configuration (`backend/.env`)

| Variable | Default | Notes |
|---|---|---|
| `GEMINI_API_KEY` | — | chat LLM, VLM and embeddings (Gemini 2.5 Flash, gemini-embedding-001) |
| `OPENAI_COMPAT_API_KEY` / `OPENAI_COMPAT_BASE_URL` | Groq URL | **Groq**, **xAI Grok**, or local **Ollama** (`http://127.0.0.1:11434/v1`, key `ollama`) for chat/vision |
| `OPENAI_COMPAT_REASONING_EFFORT` | — | e.g. `none` to switch off "thinking" on reasoning models (much faster) |
| `LLM_PROVIDER` / `VLM_PROVIDER` / `EMBEDDING_PROVIDER` | `auto` | force `gemini`, `openai_compat`, `hashing` |
| `LLM_MODEL` / `VLM_MODEL` / `EMBEDDING_MODEL` | provider default | e.g. `llama-3.3-70b-versatile` |
| `SEARCH_PROVIDER`, `TAVILY_API_KEY` | `auto` | Tavily if a key is set, else DuckDuckGo |
| `DATABASE_URL` | `embedded` | or `postgresql://user:pass@host/db` |
| `WINDOW_MINUTES` | 15 | sensor aggregation window |
| `LLM_DIGEST_ENABLED` | false | optional LLM-written daily interpretation chunk |
| `DEVICE_KEY` | `gaia-rover-dev-key` | rover telemetry auth (must match the firmware) |
| `API_KEY` | — | optional key required from the app/website |
| `FARM_TIMEZONE`, `FARM_REGION`, `FARM_NAME`, `FARMER_NAME` | Asia/Kolkata, India | |

Without any API key the system still runs: chat answers with the retrieved facts directly (no LLM),
retrieval uses an offline embedder, and diagnosis reports that a VLM must be configured.

## API overview (`/api/v1`)

| Area | Endpoints |
|---|---|
| Telemetry | `POST /telemetry` (X-Device-Key; readings, `sensor_switches`, or a heartbeat with no readings), `GET /telemetry/latest`, `/telemetry/sensor-state` (which sensors are on/off), `/telemetry/history`, `/telemetry/summary?range=day:0`, `GET /sensors` |
| Pipeline A | `POST /chat {message, mode: farm\|open, session_id}`, **`POST /chat/stream`** (same request, Server-Sent Events: `session`, `stage`, `delta`, `final`, `error`), `GET /chat/sessions/{id}` |
| Pipeline B | `POST /diagnoses` (multipart image → 202 job), `GET /diagnoses/{id}` (poll `stage`), `POST /diagnoses/from-rover`, `GET /diagnoses/{id}/image`, `DELETE /diagnoses/{id}` |
| Knowledge | `GET /knowledge`, `/knowledge/search?q=` (hybrid scores), `/knowledge/stats`, `POST /knowledge/notes`, `POST /knowledge/documents` (PDF/TXT/MD) |
| Ingestion | `POST /ingest/sync`, `/ingest/reindex`, `/ingest/digest`, `GET /ingest/status` |
| Rover relay | `GET /rover/status`, `POST /rover/drive`, `/rover/stop`, `/rover/arm`, `GET/POST /rover/sensors` (per-sensor switches), `GET /rover/capture`, `POST /rover/telemetry/pull` |
| Live view | `GET /live/state` (window progress, counters, recent events), `GET /live/stream` (Server-Sent Events: one `activity` event per thing the system does) |
| System | `GET /health`, `/config`, `/overview` |

## Tests and evaluation

```bash
cd backend && .venv\Scripts\python -m pytest            # backend incl. PostgreSQL + pgvector integration
.venv\Scripts\python -m scripts.evaluate_rag --offline   # RAG evaluation on a synthetic farm
.venv\Scripts\python -m scripts.evaluate_vlm             # VLM on 60 labelled PlantVillage leaf photos
cd ../frontend && flutter analyze && flutter test   # app
```

To verify a cloud model end to end, put `GEMINI_API_KEY` in `backend/.env` and run both evaluations with
reports (`--sleep 6` keeps the VLM run under the free tier's requests-per-minute limit):

```bash
.venv\Scripts\python -m scripts.evaluate_rag --out ..\docs\evaluation-gemini.json
.venv\Scripts\python -m scripts.evaluate_vlm --sleep 6 --out ..\docs\evaluation-vlm-gemini.json
```

## Responsible AI

Model confidence is always shown as a rough, uncalibrated indicator; `uncertain` is a first-class
result with recapture guidance; diagnoses are phrased as possibilities with visible evidence; every
farm-specific statement in chat is cited, numbers come from exact statistics, hallucinated citations are
removed, and general knowledge is flagged. Details in [ARCHITECTURE.md](ARCHITECTURE.md#7-responsible-ai).

![Web interface](docs/screenshots/web/overview.png)
