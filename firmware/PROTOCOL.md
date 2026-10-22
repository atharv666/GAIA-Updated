# GAIA rover protocols

Two interfaces:

1. **HTTP API on the ESP32-CAM** — used by the mobile app, the backend relay and the web UI.
   `backend/scripts/rover_simulator.py` implements the same API for testing without hardware.
2. **UART line protocol between the ESP32-CAM and the Arduino Nano** — the Nano owns motors and sensors.

```text
 Phone app / backend ──Wi-Fi HTTP──► ESP32-CAM ──UART 57600──► Arduino Nano ──► L298N ──► 6 × BO motors
                                        │                          ├─► DHT11, MQ5, MQ6 (MQ2 optional)
                                        │                          ├─► soil probe (on servo arm)
                                        │                          └─► HC-SR04 ultrasonic
                                        └─► camera (MJPEG :81, stills :80)
```

---

## 1. ESP32-CAM HTTP API

Port **80** (control + stills) and port **81** (MJPEG stream; a separate server so streaming never
blocks control). All JSON responses carry `Access-Control-Allow-Origin: *`.

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/status` | – | device, firmware, uptime, Wi-Fi, Nano link, drive, arm, obstacle, battery, backend push state |
| GET | `/api/telemetry` | – | latest reading in the backend's telemetry shape (below) |
| POST | `/api/drive` | one of the forms below | `{"ok":true,"left":..,"right":..}` or `{"ok":true,"held":"soil probe retracting"}` / `{"ok":false,"blocked":"obstacle"}` |
| POST | `/api/stop` | – | `{"ok":true}` |
| POST | `/api/arm` | `{"action":"deploy"}` or `{"action":"retract"}` | `{"ok":true,"arm":"deploying"}` |
| POST | `/api/config` | `{"obstacle_cm":20,"max_pwm":230}` (either field) | `{"ok":true}` |
| GET | `/api/sensors` | – | `{"switches":{"dht11":true,"soil_moisture":true,"mq5":false,"mq6":true},"warming":["mq6"],"installed":[...]}` |
| POST | `/api/sensors` | `{"mq5":false}`, `{"switches":{"mq5":false,"dht11":true}}` or `{"all":false}` (video only) | the same object plus `"ok":true`; `400` for an unknown or unfitted sensor |
| GET | `/capture` | `?flash=1` optional | high-resolution JPEG (UXGA 1600×1200 by default) |
| GET | `/snapshot` | – | one JPEG at stream resolution |
| GET | `:81/stream` | – | `multipart/x-mixed-replace` MJPEG (VGA by default) |

### Drive command forms

```json
{"left": 180, "right": 180}              // tank drive, -255..255 per side
{"throttle": 0.8, "steer": -0.3}          // joystick, -1..1; mixed as left=t+s, right=t-s
{"dir": "forward", "speed": 180}          // forward | reverse | left | right | stop
```

**Heartbeat / failsafe.** Clients repeat the drive command every 100–150 ms while a control is held.
The ESP32 stops the motors if no drive command arrives for 500 ms, and the Nano independently stops
them after 700 ms without a command (covers an ESP32 crash or Wi-Fi drop).

### Telemetry shape (`/api/telemetry` and each reading pushed to the backend)

```json
{
  "device_id": "rover-01",
  "uptime_ms": 123456,
  "ts": 1790000000,
  "values": {"temperature": 27.4, "humidity": 61.0, "soil_moisture": 43.5,
             "mq2": 172, "mq5": 208, "mq6": 191, "distance_cm": 84, "battery_v": 7.86},
  "state": {"moving": false, "arm_deployed": true, "soil_valid": true, "obstacle": false, "failsafe": false,
            "sensor_switches": {"dht11": true, "soil_moisture": true, "mq5": true, "mq6": true}, "warming": []}
}
```

### Sensor switches (power saving)

Every sensor can be switched on or off from the app: `dht11` (temperature + humidity), `soil_moisture`, `mq5`,
`mq6` (and `mq2` if fitted). The ultrasonic sensor is not switchable: it protects the rover while driving.

* A switched-off sensor produces **no readings** (its keys are absent from `values`).
* Switching an MQ sensor **on** restarts its heater warm-up (`MQ_WARMUP_MS`, 60 s): it appears under `warming`
  and has no readings until the warm-up has passed.
* Switching the soil probe off lifts the arm and refuses further `deploy` requests, so the servo stays still.
* A switch only saves battery if the sensor's supply goes through an electronic switch driven by
  `PIN_*_POWER` in `nano_controller/config.h`. Without one, the sensor is only ignored in software and its
  heater still draws current. Never power a sensor straight from a Nano pin.
* `{"all": false}` is **video-only mode**: every sensor off, camera and driving unaffected.
* Every reading carries `state.sensor_switches`, and every push to the backend does too (below), which is how
  the backend can tell "switched off on purpose" from "no data".

* `ts` (unix seconds) is present only after NTP sync; otherwise the backend reconstructs time from
  `uptime_ms` and the batch's `sent_uptime_ms`.
* `soil_moisture` is present only when the probe arm is deployed and settled and the rover is still.
* MQ values are omitted during the heater warm-up period after power-on.

### Push to the backend

Every 15 s (when Wi-Fi has internet/LAN access to the backend), and immediately after a sensor is switched,
the ESP32 POSTs queued readings. A push with an empty `readings` list is a **heartbeat**: it tells the backend
the rover is connected and which sensors are on, even in video-only mode.

```http
POST {BACKEND_URL}/api/v1/telemetry
X-Device-Key: <DEVICE_KEY>
Content-Type: application/json

{"device_id": "rover-01", "sent_uptime_ms": 130000,
 "sensor_switches": {"dht11": true, "soil_moisture": true, "mq5": false, "mq6": false},
 "readings": [ {...}, {...} ]}
```

Readings are sampled every 5 s and kept in a ring buffer (about 30 minutes) while the backend is
unreachable. When the phone is connected to the rover's own hotspot (no internet), the app relays
`/api/telemetry` to the backend instead (Settings → "Relay rover telemetry").

---

## 2. ESP32 ↔ Nano UART protocol

57600 baud, 8N1, ASCII lines. Every frame is `$<BODY>*<CS>\n` where `CS` is the XOR of all
characters of `BODY` as two uppercase hex digits (NMEA style). Frames with a bad checksum are ignored.

### Commands (ESP32 → Nano)

| Frame body | Meaning |
|---|---|
| `D,<left>,<right>` | set wheel targets, -255..255 per side (also the failsafe heartbeat) |
| `S` | stop immediately |
| `A,1` / `A,0` | deploy / retract the soil-probe arm |
| `C,OBS,<cm>` | obstacle stop distance (0 disables) |
| `C,MAX,<pwm>` | maximum motor PWM (60..255) |
| `W,<name>,<0\|1>` | switch a sensor on/off. `<name>`: `dht11`, `soil`, `mq5`, `mq6`, `mq2` or `all`. Reply `K,W,<name>,<v>`, or `E,W,name` / `E,W,notfitted` |
| `P` | ping |

The Nano answers `K,<cmd>[,<info>]` (ack) or `E,<cmd>,<reason>` (rejected), e.g. `E,A,moving`.

### Telemetry (Nano → ESP32, 2 Hz)

```
T,<seq>,<temp×10>,<hum×10>,<mq2>,<mq5>,<mq6>,<soil×10>,<dist_cm>,<arm>,<batt_mV>,<flags>,<inst>,<en>,<warm>
```

| Field | Notes |
|---|---|
| `temp×10`, `hum×10` | DHT11, `-9999` when the sensor is off or the last read failed |
| `mq2`, `mq5`, `mq6` | raw 10-bit ADC (0–1023); `-1` when that sensor is not fitted, switched off, or still warming up. The ESP32 then omits it from the telemetry |
| `soil×10` | soil moisture % × 10, `-1` unless the probe is on, the arm is deployed + settled and the rover is still |
| `dist_cm` | HC-SR04 distance, `-1` when there is no echo |
| `arm` | 0 retracted, 1 deploying, 2 settling, 3 deployed, 4 retracting |
| `batt_mV` | battery voltage via divider on A6 |
| `flags` | bit0 moving · bit1 obstacle stop active · bit2 failsafe tripped · bit3 DHT on and ok · bit4 every switched-on MQ sensor is warm · bit5 soil valid |
| `inst`, `en`, `warm` | sensor masks, one bit per sensor: bit0 `dht11`, bit1 `soil`, bit2 `mq5`, bit3 `mq6`, bit4 `mq2`. `inst` = fitted, `en` = switched on, `warm` = ready to give readings (MQ heater warm-up done; always set for the others while on) |

Firmware older than the switches sends only the first 11 fields; the ESP32 and `nano_console.py` accept both.

Example: `$T,812,274,610,-1,208,191,435,84,3,7860,56,15,15,15*75` (flags 56 = DHT ok + MQ warm + soil valid;
masks 15 = DHT11, soil, MQ5, MQ6 fitted, on and ready; MQ2 not fitted)

Same rover with MQ5 and MQ6 switched off: `$T,813,274,610,-1,-1,-1,435,84,3,7860,56,15,3,3*77`
