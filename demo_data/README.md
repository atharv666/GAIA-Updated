# GAIA demo data

Everything needed to demonstrate the whole system **without the rover**: a few days of rover sensor history,
farmer notes, farm documents, and crop photos for the vision-language diagnosis. A loader script feeds all of it
through the backend's real API, so it also shows how data gets in and where it ends up.

```text
demo_data/
├── load_demo_data.py          loads everything into a RUNNING backend over HTTP
├── notes/                     5 farmer notes (.md)            -> POST /knowledge/notes
├── documents/                 3 farm documents (.md/.txt)     -> POST /knowledge/documents
├── plant_photos/              4 tomato leaf photos (early blight, late blight, Septoria, healthy) -> POST /diagnoses
└── sample_payloads/           format reference: what the rover sends and what is stored (see below)
```

## Run it

1. Put your key in `backend/.env`: `GEMINI_API_KEY=...` (embeddings, chat and vision all use it).
2. Start the backend, listening on your network so the phone can reach it:

   ```powershell
   cd backend
   .venv\Scripts\uvicorn gaia.main:app --host 0.0.0.0 --port 8000
   ```
3. In a second terminal, load the demo data:

   ```powershell
   cd backend
   .venv\Scripts\python ..\demo_data\load_demo_data.py --backend http://localhost:8000
   ```

   It takes a couple of minutes. The four diagnoses run one after another (each is one vision call, a few
   agent calls and a few web searches) with an 8-second pause between photos to respect free-tier rate limits.
   Options: `--skip-diagnoses`, `--days 7`, `--reindex`, `--device-key`, `--api-key` (`--help` lists all).
4. Open the app, **Settings**, and set *GAIA server URL* to `http://<your PC LAN IP>:8000` (phone and PC on the
   same Wi-Fi; allow port 8000 through the Windows firewall). `http://10.0.2.2:8000` is for an Android emulator.

Re-running is safe: notes, documents and photos already present are skipped, and each run adds fresh sensor
history ending "now", so "today", "this morning" and "yesterday" always have data.

If you loaded the data **before** adding the key, vectors were made with the offline fallback embedder. Add the
key, restart the backend and run the loader again with `--reindex`.

What works without the rover: Overview, Sensors (values, charts), Gaia AI chat (both modes), Diagnose (camera,
gallery, or the sample photos). The Rover tab will show "offline" until the rover is connected.

Tip: open the app's **Live** tab before running the loader to watch the readings arrive, the windows close, and
each chunk being written, embedded and stored.

## What the loader does, step by step

| Step | Request | Stored where |
|---|---|---|
| 1. Sensor telemetry: 4 days of rover runs (06:00-09:00 and 16:00-18:30 farm time; soil only while "parked"; a methane spike on the second day's evening run, near the biogas unit). The last batch is sent in the ESP32's no-clock form. | `POST /api/v1/telemetry` with `X-Device-Key` | `sensor_readings` (one row per sensor per reading) |
| 2. Notes | `POST /api/v1/knowledge/notes` | `farm_documents` + embedded chunks (`farm_note`) |
| 3. Documents | `POST /api/v1/knowledge/documents` | `farm_documents` + embedded chunks (`document`) |
| 4. Photos (needs the key) | `POST /api/v1/diagnoses`, then polls `GET /api/v1/diagnoses/{id}` | photo file on disk, record in `diagnoses`, embedded chunks (`diagnosis`, `agent_finding`) |
| 5. Window summaries | `POST /api/v1/ingest/sync` | `sensor_windows`, then embedded chunks (`sensor_window`, `sensor_daily`) |

The rover itself is not simulated here. The telemetry is posted exactly as the ESP32 would post it. The demo
sensors are DHT11, soil probe, MQ5 and MQ6.

The demo also includes **power-saving choices on purpose**, so you can show how the AI explains gaps: this
morning's run has the **gas sensors switched off**, yesterday evening's run has the **soil probe switched off**,
and there is a short **video-only** stretch (every sensor off). Each reading carries its `sensor_switches`, exactly
like the real firmware.

## Format reference (`sample_payloads/`)

Follow the data from the wire to the knowledge store:

| File | What it shows |
|---|---|
| `nano_uart_frame.txt` | The line the Arduino Nano sends the ESP32 every 500 ms, decoded field by field. |
| `rover_api_telemetry_response.json` | What the rover returns for `GET /api/telemetry` (the phone relay reads this). |
| `esp32_post_uptime_only.json` | The body the ESP32 POSTs to `/api/v1/telemetry` when it has no clock: only `uptime_ms`, and the backend stamps each reading from its receive time. |
| `esp32_post_with_clock.json` | The same with an NTP `ts` per reading (unix seconds). |
| `stored_sensor_window_chunk.json` | A 15-minute window as stored in the knowledge store: template text (the part that is embedded) plus exact statistics in `meta`. |
| `stored_sensor_daily_chunk.txt` | The daily rollup text. |
| `stored_farm_note_chunk.json` | A farmer note as stored. |
| `api_telemetry_latest_response.json` | What the app's Sensors screen reads (`GET /api/v1/telemetry/latest`). |
| `api_telemetry_history_response.json` | What the charts read (`GET /api/v1/telemetry/history`), first points only. |

The two `esp32_post_*` files are valid requests; you can post them with curl to try the endpoint:

```powershell
curl -X POST http://localhost:8000/api/v1/telemetry -H "X-Device-Key: gaia-rover-dev-key" -H "Content-Type: application/json" --data-binary "@demo_data/sample_payloads/esp32_post_uptime_only.json"
```

The `stored_*` and `api_*` files are real output captured from a run of the loader (their dates are from that run).

## Suggested demo script

**Query my farm**
1. "What is the current temperature and humidity?" - latest reading, exact numbers with a citation.
2. "How humid was my field this morning?" - exact statistics for the morning window.
3. "Was the soil moisture lower today than yesterday?" - comparison computed in code.
4. "Were there any unusual gas readings in the last 4 days?" - finds the methane spike near the biogas unit.
5. "Where is the biogas unit and why are MQ5 readings higher there?" - answered from the farmer note.
6. "What is my irrigation schedule for the north plot?" - from the notes.
6b. "What were the gas readings this morning?" - the gas sensors were switched off: the AI says so, gives the
    on/off periods and the tip to switch them on. "How was the soil moisture yesterday evening?" does the same
    for the soil probe. "When was the rover collecting sensor data today?" reads the sensor on/off log.
7. "What crop problems have been found in my field?" - from the diagnoses (loader step 4).
8. "What were the conditions when the early blight was detected?" - links the diagnosis time to the sensor log.

**Open-ended:** "How do I prevent early blight in tomatoes?" - web search with sources.

**Diagnose:** upload `early_blight_tomato.jpg` (or take a new photo). Show the stages (vision, searching,
writing), the evidence, the confidence caveat and the sources, then ask chat about it.
The loader's diagnoses are timestamped when the loader ran. For question 8 the sensor history must cover that
moment, so run the loader shortly before the demo, or diagnose a photo live.

Photo credits: [plant_photos/ATTRIBUTION.md](plant_photos/ATTRIBUTION.md).
