# GAIA backend

FastAPI service that runs both Generative AI pipelines, the shared knowledge store (PostgreSQL +
pgvector), the sensor ingestion jobs, the web interface, and a rover relay.

```text
gaia/
├── main.py                  FastAPI app: /api/v1 + web interface (lifespan starts the scheduler)
├── config.py                settings from backend/.env
├── container.py             wires providers + services; background ingestion scheduler
├── db/                      models, portable types (UTC datetimes, vector(768) on PG / JSON on SQLite), engine
├── core/                    sensor registry, farm-time utilities, text chunker
├── providers/               LLM/VLM (Gemini, OpenAI-compatible: Groq/xAI), embeddings, web search
├── ingestion/               telemetry validation, window aggregation, document builders, sync service
├── knowledge/               knowledge store (hybrid pgvector + full-text, RRF), note/document indexing
├── pipeline_a/              query planner, exact sensor facts, farm RAG, open-ended web chat, sessions
├── pipeline_b/              VLM analysis (strict JSON), single search agent, diagnosis jobs + knowledge write
├── rover/                   HTTP client for the ESP32-CAM API
├── api/                     REST routers
└── web/                     Jinja2 templates + static JS/CSS for the website
scripts/
├── seed_demo_data.py        backfill realistic rover sessions (+ farmer notes)
├── rover_simulator.py       emulates the ESP32-CAM API (stream, capture, drive, arm, telemetry push)
├── evaluate_rag.py          Pipeline A evaluation on a deterministic synthetic farm
├── evaluate_vlm.py          Pipeline B vision benchmark on 60 labelled PlantVillage leaf photos
├── nano_console.py          USB bring-up console + automated checks for the Arduino Nano
├── rover_check.py           step-by-step check of a rover (or the simulator) over its HTTP API
└── farm_model.py            synthetic field conditions
tests/                       pytest suite (SQLite + real PostgreSQL/pgvector integration)
```

## Run

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements-local.txt
copy .env.example .env                                   # set GEMINI_API_KEY
.venv\Scripts\uvicorn gaia.main:app --host 0.0.0.0 --port 8000
```

Useful commands:

```powershell
.venv\Scripts\python -m scripts.seed_demo_data --days 3 --notes
.venv\Scripts\python -m scripts.rover_simulator --port 8090 --backend http://localhost:8000 --capture-dir C:\path\to\leaf\photos
.venv\Scripts\python -m scripts.evaluate_rag --out ..\docs\evaluation.json
.venv\Scripts\python -m scripts.evaluate_vlm --out ..\docs\evaluation-vlm.json
.venv\Scripts\python -m scripts.nano_console COM5 --check        # Nano on USB (see firmware/README.md §6)
.venv\Scripts\python -m scripts.rover_check http://192.168.4.1   # assembled rover over Wi-Fi
.venv\Scripts\python -m pytest
```

`--capture-dir` makes the simulator's `/capture` return your own leaf photos, so Pipeline B can be
exercised end-to-end ("Capture & diagnose" on the website's Rover page).

## Notes

* **Embedded PostgreSQL** (`DATABASE_URL=embedded`) keeps its data in `backend/data/pgdata`. Stop the
  server with Ctrl+C for a clean shutdown; after a crash the next start waits for Postgres recovery
  (up to two minutes) automatically.
* **Changing the embedding model** (provider, model or `EMBEDDING_DIM`): run `POST /api/v1/ingest/reindex`.
  Changing `EMBEDDING_DIM` on an existing PostgreSQL database also requires recreating the
  `knowledge_chunks` table (the vector column has a fixed dimension).
* **Time zone:** windows and "today / this morning" follow `FARM_TIMEZONE`; everything is stored in UTC.
* The rover pushes telemetry with `X-Device-Key: $DEVICE_KEY`. If `API_KEY` is set, the app and the
  website must send `X-API-Key` (the website asks for it once and stores it in the browser).
