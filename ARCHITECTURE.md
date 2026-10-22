# GAIA architecture

GAIA is a rover-assisted **Generative AI** system for proactive crop-health monitoring. The rover is a
thin data-acquisition layer; the intelligence lives in **two independent GenAI pipelines** that share one
**knowledge store**.

```text
          ┌────────────── Rover (ESP32-CAM + Arduino Nano) ──────────────┐
          │ DHT11 · MQ5 · MQ6 (MQ2 optional) · soil probe on servo arm · HC-SR04│
          │ MJPEG live view (:81)  ·  UXGA stills (/capture)            │
          └───────────────┬──────────────────────────────┬──────────────┘
             telemetry (HTTP push / app relay)      high-res still / phone photo
                          │                              │
┌─────────────────────────▼────────────┐   ┌─────────────▼──────────────────────────┐
│ Ingestion (deterministic)            │   │ Pipeline B — VLM crop diagnosis        │
│ validate → store → window aggregates │   │ image → VLM (strict JSON)              │
│ → window / daily documents → embed   │   │       → ONE research agent (web search)│
└─────────────────────────┬────────────┘   │       → present + write knowledge      │
                          │                └─────────────┬──────────────────────────┘
                          ▼                              │ one-way write
┌──────────────────────────────────────────────────────▼─────────────────────────┐
│ Shared knowledge store — PostgreSQL + pgvector (HNSW) + full-text (GIN)        │
│ sensor_window · sensor_daily · sensor_digest_llm · diagnosis · agent_finding   │
│ farm_note · document                         + exact sensor tables (SQL)       │
└──────────────────────────────────────────────────────┬─────────────────────────┘
                                                       │ read only
┌──────────────────────────────────────────────────────▼─────────────────────────┐
│ Pipeline A — conversational assistant                                          │
│  "Query my farm": plan → exact SQL facts [F#] + hybrid retrieval [K#] → LLM    │
│  "Open-ended":    plan → web search [W#] → LLM                                 │
└────────────────────────────────────────────────────────────────────────────────┘
      ▲ Flutter mobile app (frontend/)         ▲ Web interface (backend/gaia/web)
```

**Independence (context.md constraints 1–6).** Pipeline B never reads sensor tables or chat history, and
Pipeline A never sees image pixels. The only link is Pipeline B *writing* `diagnosis` and
`agent_finding` chunks that Pipeline A may later *read*. There is no control loop. This is verified by a
test that records every SQL statement issued while a diagnosis runs and asserts none touches
`sensor_*` or `chat_*` tables (`backend/tests/test_pipeline_b.py`).

---

## 1. Sensor ingestion — the design decision

The brief asked whether readings should be summarised by code (min / max / average per time frame) or
by an LLM. GAIA uses **both, in layers, with a strict rule: numbers only ever come from code.**

| Layer | Source type | Produced by | Purpose |
|---|---|---|---|
| Raw log | `sensor_readings` table | validation code | ground truth, long format (any new sensor key just works) |
| Window aggregates | `sensor_windows` table | code: count/min/max/mean/std/first/last + min/max timestamps per 15-min window | exact statistics for SQL retrieval |
| 1 · Window document | `sensor_window` chunk | code template | semantic retrieval of *when* something happened ("gas spike", "very humid early morning") |
| 2 · Daily rollup | `sensor_daily` chunk | code | extremes with times, time-of-day profile, threshold events |
| 2b · Sensor on/off log | `sensor_sessions` chunk | code, from the switch events | when the rover was connected and which sensors were on or off, per day |
| 3 · Daily interpretation *(optional)* | `sensor_digest_llm` chunk | LLM, from the Layer 2 text only | richer semantic phrasing (e.g. "conditions favoured fungal disease") — labelled *AI-generated interpretation (not measurements)* |

Why not an LLM per window: ~96 calls a day per rover, added latency and cost, and — most importantly —
the risk of an LLM altering numbers that farmers act on. Deterministic templates already embed well; the
optional digest adds interpretation without being trusted for facts.

Mechanics (`backend/gaia/ingestion/`):

* **Validation:** unknown keys normalised (`temp` → `temperature`), NaN / out-of-range values rejected,
  **soil moisture dropped unless the probe arm is deployed and the rover is still**, duplicates ignored
  with `INSERT … ON CONFLICT DO NOTHING RETURNING`.
* **Timestamps without an RTC:** the ESP32 may send `uptime_ms` per reading plus `sent_uptime_ms` per
  batch; the backend reconstructs wall-clock time from its own receive time. NTP `ts` is used when valid.
* **Dirty windows:** each insert marks its window dirty (`marked_at` upsert). A background scheduler
  aggregates closed windows, writes documents, embeds them, and clears the flag *only if `marked_at` is
  unchanged* — late-arriving data (store-and-forward from the rover) is re-aggregated, never lost.
* **Freshness on demand:** every "Query my farm" question first runs `sync(include_open=True)`, so the
  still-filling window is aggregated too. This is the "ingestion phase runs, then the retriever runs"
  flow from the brief.
* **Unchanged content is not re-embedded** (content hash per chunk), and every chunk records its
  `embed_model`, so a model change never mixes vector spaces (`POST /ingest/reindex` re-embeds).

### Sensor switches and gaps

Every sensor (DHT11, soil probe, MQ5, MQ6, MQ2 if fitted) can be switched on or off from the app to save
battery. A switched-off sensor records nothing, so "no data" needs a *reason*. The reason is recorded by code,
never guessed by the LLM (`backend/gaia/ingestion/coverage.py`):

* The rover reports its switches with every reading and every push (`sensor_switches`); a push with no
  readings is a **heartbeat** that shows the rover is connected even in video-only mode. The backend stores
  only the *changes* (`sensor_state_events`) and one row per device per 5-minute bucket (`rover_heartbeats`).
* From these records plus the readings themselves, every sensor in every 5-minute bucket gets one status:
  **recorded**, **off** (switched off by the farmer), **no_reading** (on, but warming up / probe not lowered /
  moving), **offline** (rover not connected) or **unknown** (no switch information, e.g. older firmware).
* **Chunks say what they lack:** a window chunk lists each missing sensor ("MQ5 gas index: not recorded in
  this window - sensor switched off"); the daily summary adds a "Sensor on/off during the day" line; the
  `sensor_sessions` chunk lists the day's sessions ("07:00-08:00: sensors on: DHT11, soil probe; switched
  off: MQ5, MQ6", "rover connected with every sensor off (video only)").
* **Facts at question time:** for the sensors and period in a question the planner already extracted, a
  `switches` fact states when each was recorded, off, silent or disconnected (short periods as time spans,
  ranges over 36 h as totals). "Latest reading" facts say "this sensor is switched off now (since ...)"
  instead of the generic "rover may be offline", and a **Tip** ("switch MQ5 on in the app (Rover tab) to
  record it; gas sensors need a few minutes to warm up") is attached when the sensor is off.
* **Prompt rule:** quote those periods exactly, never estimate or imply values for them, say which period an
  average covers, pass on the tip, and for advice that needs a sensor that was off, say the data is missing
  and give clearly labelled general guidance.
* Old firmware that sends no switches behaves exactly as before: no `switches` facts, no sessions chunk.

## 2. Pipeline A — "Query my farm"

`backend/gaia/pipeline_a/farm_assistant.py`

1. **Ingestion sync** (above).
2. **Query understanding** — the LLM returns a `QueryPlan` (structured output): intent
   (`current_status`, `sensor_statistics`, `comparison`, `trend`, `diagnosis_history`, `farm_knowledge`,
   `general_advice`, `small_talk`), sensor keys, time tokens, whether to search knowledge, whether to
   link conditions to a diagnosis, and a standalone rewrite that resolves follow-ups ("what about
   yesterday?"). **The LLM never computes dates**: it emits tokens such as `day:-1`,
   `daypart:0:morning`, `last_days:7`, which `core/timeutil.py` resolves against the farm's time zone.
   A rule-based planner produces the same structure when no LLM is configured or the call fails.
3. **Structured retrieval → `[F#]` facts** (`sensor_facts.py`): exact SQL over window aggregates
   (count-weighted means, true extremes with timestamps), comparisons and deltas computed in code,
   time-of-day / per-day profiles for trends, latest values with staleness warnings, "no data for X"
   facts and log-coverage facts so the model can say *what it does not know*.
4. **Semantic retrieval → `[K#]` chunks** (`knowledge/store.py`): pgvector cosine search **plus**
   PostgreSQL full-text search, fused with **Reciprocal Rank Fusion** (k = 60). Filters by source type
   (e.g. only diagnoses for diagnosis questions) and time overlap; when exact facts already cover a
   numeric question, per-window chunks are skipped in favour of daily summaries and notes.
5. **Multi-hop** — "What conditions were recorded when this issue was detected?": retrieved
   `diagnosis` chunks carry their timestamp; sensor facts are computed for ±1 h around it.
6. **Grounded generation** — the LLM returns `GroundedAnswer {answer, cited, used_general_knowledge,
   insufficient_data, follow_ups}`. Rules: every farm-specific statement cites `[F#]`/`[K#]`, numbers are
   quoted from facts, diagnoses stay "possible", general advice goes in a separate "General guidance"
   paragraph, MQ values are never presented as ppm.
7. **Citation validation** — citation IDs that do not exist in the context are stripped from the answer
   (hallucinated-citation guard); the API returns only sources that were actually cited, plus grounding
   flags (farm data / knowledge store / general knowledge / insufficient data) that the UIs show as badges.

**Streaming.** `POST /chat/stream` runs the same pipeline and sends Server-Sent Events: `stage`
(syncing → planning → retrieving / searching → generating), `delta` (answer text while the model writes it)
and `final` (the complete reply with validated citations, sources and grounding flags, which replaces the
streamed draft). The model still produces one structured JSON object in a single call; the backend decodes
the `answer` field incrementally (`pipeline_a/streaming.py`) so the app shows words as they are generated
without a second LLM call. Both providers stream natively (Gemini `generate_content_stream`, OpenAI-compatible
`stream: true`); the app falls back to `POST /chat` against an older server.

**Graceful degradation:** without an LLM the same retrieval runs and the answer lists the retrieved facts
extractively; without embeddings an offline hashing embedder keeps hybrid search working.

### Open-ended mode

`open_assistant.py`: the LLM plans 0–3 web queries (adding the farm's region where relevant) → Tavily or
DuckDuckGo in parallel → de-duplicated results ranked with institutional sources (.edu/.gov/.ac.in,
ICAR, FAO, CABI, extension services …) first → grounded answer with `[W#]` citations → validation.

## 3. Pipeline B — VLM crop diagnosis

`backend/gaia/pipeline_b/`

1. **Image preparation:** validation with Pillow (rejects non-images and decompression bombs), EXIF
   orientation, resize to ≤1600 px JPEG. The farmer's optional note is stored as metadata but **not**
   shown to the model, keeping the pipeline image-only.
2. **One VLM call → strict JSON** (`VLMDiagnosis`): `is_plant_image`, `plant`, `health_status`
   (`healthy | unhealthy | uncertain`), `diagnosis` (phrased as a possibility), `issue_categories`
   (disease, nutrient deficiency, pest damage, fungal, bacterial, viral, environmental stress, physical
   damage, abnormal coloration/morphology), `evidence` (observable only), `assumptions` (inferences),
   `differential`, `severity`, `confidence`, `image_quality`. Gemini uses native response schemas;
   OpenAI-compatible models use JSON mode + schema-in-prompt + one self-repair attempt. Invariants are
   re-enforced in code (e.g. a non-plant image is always `uncertain`).
3. **One research agent** (`agent.py`) with a web-search tool, bounded to `AGENT_MAX_ROUNDS` rounds of
   plan → search → observe. The agent decides queries and when to stop; the branch policy is
   healthy → cultivation/care, unhealthy → condition/treatment/prevention, **uncertain → treated like
   unhealthy plus distinguishing symptoms**, with the uncertainty preserved. If the planner returns no
   queries, deterministic fallback queries are used. It then writes `AgentFindings` with `[W#]` citations,
   which are validated like chat citations.
4. **Present + augment:** the result shows plant, status, evidence, the confidence with its caveat,
   recapture tips (for uncertain / poor images), findings and sources. The diagnosis and the findings
   are written to the knowledge store with provenance (URLs, domains, model IDs, `web_grounded`).

Jobs are asynchronous (`202 Accepted` + polling of `stage`: `queued → vlm → searching → writing → done`)
so slow mobile networks never hold a long HTTP request; interrupted jobs are marked failed on restart.

## 4. Knowledge store schema (PostgreSQL + pgvector)

| Table | Key columns |
|---|---|
| `sensor_readings` | device_id, sensor, ts, value — unique (device, sensor, ts) |
| `sensor_windows` | device_id, sensor, window_start/end, count, min/max/avg/std, first/last, min_ts/max_ts |
| `dirty_windows` | device_id, window_start, marked_at |
| `knowledge_chunks` | source_type, source_id, chunk_index, title, content, **embedding vector(768)** (HNSW, cosine), embed_model, time_start/end, meta (JSONB), content_hash; GIN full-text index |
| `diagnoses` | image path + sha256, source, status/stage, VLM JSON, findings, sources, agent trace, knowledge chunk IDs, timings |
| `farm_documents` | farmer notes and uploaded PDFs/text (chunked with overlap) |
| `chat_sessions`, `chat_messages` | conversation memory with sources and grounding per answer |

`DATABASE_URL=embedded` starts a real PostgreSQL 16 + pgvector 0.6 locally through `pgserver` (no Docker);
Docker Compose uses the `pgvector/pgvector:pg16` image. SQLite works for tests (vector and lexical search
then run in Python).

### Live activity stream

`core/activity.py` keeps the last 400 events in memory (not persisted). The pipeline publishes an event at each
step: `readings_received` (telemetry endpoint), `window_closed` (ingestion), `chunk_created` -> `chunk_embedded` ->
`chunk_stored` (every write to the knowledge store, so sensor windows, daily summaries, diagnoses and notes all
show up), `chat_*` and `diagnosis_*` stages. `GET /live/stream` pushes them as Server-Sent Events and
`GET /live/state` returns the open window's progress, counters and recent events for the app's Live view.

## 5. Providers

| Role | Default | Alternatives |
|---|---|---|
| Chat LLM | Gemini 2.5 Flash | any OpenAI-compatible API: **Groq** (Llama 3.3 70B), **xAI Grok**, OpenAI, OpenRouter, local **Ollama** (tested with gemma4:e4b) |
| Vision-language model | Gemini 2.5 Flash | Groq Llama 4 Scout (vision), Grok vision models, local Ollama gemma4 (tested end to end) |
| Embeddings | gemini-embedding-001 @ 768 d (L2-normalised) | OpenAI-compatible `/embeddings`; offline hashing embedder |
| Web search | DuckDuckGo (no key) | Tavily |

All pipelines use only two provider calls — `complete()` and `structured(schema)` — so switching
providers is configuration, not code.

## 6. Rover and clients

* **Firmware** (`firmware/`): ESP32-CAM serves the HTTP API, MJPEG stream and UXGA stills, forwards
  drive commands to the Nano, samples telemetry every 5 s and pushes batches to the backend (ring buffer
  while offline). The Nano runs motors with soft-start ramping, a 700 ms command failsafe, ultrasonic
  obstacle stop, and the soil-arm state machine. Both sketches compile cleanly (see `firmware/README.md`).
* **Flutter app** (`frontend/lib/`): **Live** (animated pipeline: readings in, window filling, chunk written, embedded, stored, plus
  chat and diagnosis stages; driven by `GET /live/stream`), Overview, Rover (live MJPEG, D-pad + joystick with heartbeat, soil probe,
  capture), Diagnose (camera / gallery / rover → live pipeline stages → results + sources), Gaia AI
  (both chat modes, citations, grounding badges, follow-ups, persistent session), Sensors (latest values,
  sparklines), Settings (URLs, keys, telemetry relay for hotspot mode).
* **Website** (`backend/gaia/web/`): the same features in a browser, plus a knowledge-store retrieval
  inspector showing fused, vector and lexical scores per chunk.
* **Simulator** (`backend/scripts/rover_simulator.py`): the same HTTP API as the firmware.
* **Bring-up tools**: `nano_console.py` speaks the UART protocol to a Nano on USB (automated sensor,
  arm, interlock and failsafe checks, plus a manual console); `rover_check.py` walks the assembled rover's
  HTTP API (status, sensors, camera, stream, arm, drive and watchdog). Both are tested without hardware.

## 7. Responsible AI

| Concern | Implementation |
|---|---|
| Uncalibrated confidence | shown as "rough indicator" with a fixed caveat string from the backend on every result |
| Uncertainty | explicit `uncertain` status, conservative confidence prompt, recapture guidance, differential list, "how to confirm" section |
| Evidence vs. inference | separate `evidence` and `assumptions` fields in the VLM schema |
| No overclaiming | diagnoses phrased as possibilities everywhere (prompts, stored chunks, UI), disclaimer on each result |
| Hallucinated numbers | numbers come from SQL facts; the model is told to quote, not compute; eval measures numeric recall |
| Hallucinated sources | citation IDs validated against the retrieved context; uncited sources shown separately |
| Source quality | institutional sources ranked first and labelled; provenance stored with every agent finding |
| General vs. farm knowledge | grounding flags + "General guidance:" paragraph convention |
| Chemical safety | no exact pesticide doses; defer to label and local extension services |
| Uncalibrated sensors | MQ values labelled relative indices, never ppm; soil readings gated on probe state |
| Privacy / deletion | deleting a diagnosis removes its image and its knowledge chunks; optional API key; device key for telemetry |

## 8. Evaluation

`python -m scripts.evaluate_rag [--offline] [--out report.json]` builds a deterministic synthetic farm
(3 days of rover sessions, a gas spike, a diagnosis, farmer notes), computes ground truth **directly from
the raw readings**, and scores ten representative questions on planner accuracy, numeric recall,
citation-type accuracy, keyword accuracy and latency.

| Run | Planner | Numeric recall | Citation accuracy | Keywords | Median latency |
|---|---|---|---|---|---|
| Offline: heuristic planner, extractive answers (`docs/evaluation-offline.json`) | 100 % | 100 % | 100 % | 100 % | ~5 ms |
| Local LLM gemma4:e4b via Ollama, first run | 90 % | 83 % | 80 % | 100 % | 13.5 s |
| Local LLM gemma4:e4b, after fixes (`docs/evaluation-local-llm.json`) | 100 % | 100 % | 100 % | 100 % | 18.9 s |

The first local-LLM run exposed four failures, which led to three pipeline changes: alert-band
excursions (e.g. a short methane spike hidden by a normal average) became separate "UNUSUAL READING"
facts; deterministic planner backstops (a named past period is never `current_status`; diagnosis and
knowledge questions always search the store and link sensor conditions when asked); and the benchmark
now accepts either of two equally valid evidence types for trend questions. These are single runs on a
10-question benchmark, not a general accuracy claim; larger cloud models and a bigger question set are
the next step.

A real end-to-end Pipeline B run with local models (rover simulator serving a CC-BY photo of a tomato leaf
with early-blight lesions → gemma4 VLM → one agent with live DuckDuckGo search → knowledge write → recalled
in chat) completed in 44–86 s on a laptop RTX 4060: the VLM reported "unhealthy — possible fungal or
bacterial leaf spot" with 0.7 self-reported confidence (it did not name the crop or the specific disease,
which is typical of a small model and exactly why confidence is shown as uncalibrated and the result framed
as a possibility), and the agent cited institutional sources first (NIH/PMC, Vikaspedia, TNAU).

**Vision benchmark.** `python -m scripts.evaluate_vlm [--out report.json]` scores the VLM step on a seeded
sample of 60 labelled PlantVillage leaf photos (10 classes: tomato, potato, maize and bell pepper; 4 healthy
and 6 diseased). It reports plant identification, health-status accuracy (`uncertain` counts as not
correct and is reported separately), missed-disease and false-alarm rates, whether the labelled disease is
named in the diagnosis (top-1) or the differential (top-k), calibration of the self-reported confidence
(expected calibration error, Brier score, reliability table) and latency. The images are downloaded at run
time from the dataset's repository (Mohanty, Hughes & Salathé 2016) and are not redistributed. PlantVillage
shows single leaves on plain backgrounds, so its scores are an upper bound for rover photos. Field photos
and a small farmer usability study remain the next steps.

Automated tests: 64 backend tests (including real PostgreSQL + pgvector integration tests, provider
contract tests that run the Gemini SDK and the OpenAI-compatible client against local fake APIs, and the
rover bring-up tools against a fake Nano and the rover simulator), 7 Flutter tests, `flutter analyze`
clean, both firmware sketches compile without warnings. `.github/workflows/tests.yml` runs the backend and
app tests on every push.

## 9. Limitations and future work

* VLM confidence is self-reported and uncalibrated; the VLM can misidentify crops or diseases, especially
  on poor images. A future `uncertain` branch could require a recapture before searching.
* Web search quality varies by region and language; results are snippets, not full documents.
* MQ sensors are uncalibrated and cross-sensitive; DHT11 accuracy is ±2 °C / ±5 % RH.
* Location within the field is not tracked (no GPS/odometry); readings are per rover, not per plot.
* Single-farm, single-user design; multi-tenant auth, offline-first mobile sync and Indian-language
  (voice) interaction are natural next steps.
