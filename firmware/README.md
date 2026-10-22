# GAIA rover firmware

The rover is the thin **data-acquisition layer**: an **ESP32-CAM** handles Wi-Fi, the camera and the
HTTP API; an **Arduino Nano** handles the 6WD motors, the soil-probe arm and all sensors. They talk
over a checksummed UART protocol. Full API and wire formats: [PROTOCOL.md](PROTOCOL.md).

| Folder | Board | Role |
|---|---|---|
| [`esp32cam_rover/`](esp32cam_rover/) | AI-Thinker ESP32-CAM | Wi-Fi (STA → hotspot fallback), MJPEG stream `:81/stream`, UXGA stills `/capture`, drive/arm API, telemetry push to the backend |
| [`nano_controller/`](nano_controller/) | Arduino Nano (ATmega328P) | 6 × BO motors (skid steer) with soft ramp + failsafe, obstacle stop, servo soil arm, DHT11, MQ2/MQ5/MQ6, soil probe, battery |

Both sketches (including the per-sensor switches) were compile-verified with `arduino-cli` on a short install path (the ESP32 toolchain breaks on Windows paths over 260 characters) (ESP32 core **3.3.12**, AVR core **1.8.8**,
ArduinoJson **7.4.3**, DHT sensor library **1.4.7**, Servo **1.3.0**) with no warnings in these sources:

| Sketch | Flash | RAM |
|---|---|---|
| esp32cam_rover | 1.23 MB (39 %) | 86 KB (26 %) |
| nano_controller | 12.6 KB (41 %) | 608 B (29 %) |

> No rover yet? `python -m scripts.rover_simulator` in `backend/` serves the exact same HTTP API
> (stream, capture, drive with failsafe, arm state machine, telemetry push), so the app, backend and
> website can be tested end-to-end today and switched to the real rover by changing one URL.

---

## 1. Bill of materials

| Part | Notes |
|---|---|
| ESP32-CAM (AI-Thinker, OV2640, 4 MB PSRAM) + ESP32-CAM-MB USB programmer | camera, Wi-Fi |
| Arduino Nano (ATmega328P) | motors + sensors |
| 6WD chassis with 6 × BO geared motors | 3 per side, wired in parallel per side |
| Motor driver: L298N (minimum) — better 2 × BTS7960 or a Cytron MDD10A | 3 BO motors per channel can exceed the L298N's 2 A at stall; set `DRIVER_MODE 1` for DIR+PWM drivers |
| DHT11 | temperature + humidity |
| MQ5, MQ6 modules (analog out) | LPG/methane, LPG/butane. An MQ2 (smoke) module is optional: wire it to A0 and set `HAS_MQ2 1` in `nano_controller/config.h` |
| Capacitive soil moisture probe v1.2 | on the servo arm |
| SG90 / MG90S servo | lowers the soil probe (MG90S for firmer soil) |
| HC-SR04 ultrasonic | obstacle stop |
| 2S (7.4 V) or 3S (11.1 V) Li-ion pack + BMS | motor supply |
| 5 V ≥ 3 A buck converter (LM2596 / XL4015) | logic + sensors + servo |
| 1 kΩ + 2 kΩ resistors | 5 V → 3.3 V divider for Nano TX → ESP32 RX |
| 10 kΩ + 4.7 kΩ resistors | battery sense divider |

## 2. Wiring

### ESP32-CAM ↔ Nano (UART, 57600 baud)

| From | To | Note |
|---|---|---|
| ESP32 **GPIO14** (TX) | Nano **D0** (RX) | 3.3 V logic is read as HIGH by the Nano |
| Nano **D1** (TX) | 1 kΩ → ESP32 **GPIO13** (RX) → 2 kΩ → GND | divider protects the 3.3 V ESP32 input |
| GND | GND | **common ground is mandatory** |

GPIO12 is avoided on purpose: it is a boot strapping pin and a HIGH level at reset (the Nano's idle TX)
would stop the ESP32 from booting. The SD-card slot is unused. **Disconnect D0/D1 while uploading to the Nano.**

### Nano pin map (`nano_controller/config.h`)

| Nano | Connects to |
|---|---|
| D2 | DHT11 data (10 kΩ pull-up to 5 V if your module has none) |
| D3 | Soil-arm servo signal |
| D4, D7 | L298N IN1, IN2 (left side) |
| D5 | L298N ENA (left PWM) — remove the ENA jumper |
| D6 | L298N ENB (right PWM) — remove the ENB jumper |
| D8, D11 | L298N IN3, IN4 (right side) |
| D9, D10 | HC-SR04 TRIG, ECHO |
| D13 | status LED (solid = driving, blinking = failsafe tripped) |
| A0, A1, A2 | MQ2 (optional, `HAS_MQ2`), MQ5, MQ6 analog out |
| A3 | Soil probe analog out |
| A6 | Battery divider midpoint (battery+ → 10 kΩ → A6 → 4.7 kΩ → GND) |

Motors: the three left motors in parallel on OUT1/OUT2, the three right motors on OUT3/OUT4. If one side
spins backwards, swap its wires or set `INVERT_LEFT` / `INVERT_RIGHT`.

### Power

```text
Battery (7.4–11.1 V) ──► L298N +12V (motors)
        └──────────────► 5 V buck (≥3 A) ──► ESP32-CAM 5V, Nano 5V, MQ heaters, servo, DHT11, HC-SR04, soil probe
All grounds tied together.
```

Budget on the 5 V rail: ESP32-CAM ~0.3 A (peaks higher with Wi-Fi + camera), three MQ heaters ~0.45 A,
servo up to ~0.6 A at stall → a 3 A buck leaves headroom. ESP32-CAM brownout resets almost always mean
a weak 5 V supply or thin wires; fix the supply rather than disabling the brownout detector.

### Sensor switches (saving battery)

The app can switch each sensor on or off (DHT11, soil probe, MQ5, MQ6, MQ2 if fitted), and **Video only** turns
them all off. A switched-off sensor produces no readings; the backend records the switch, so the AI can say
*why* data is missing and suggest switching the sensor back on.

* **Software only (default):** `PIN_*_POWER` is `-1`. The sensor is ignored, but its supply stays connected, so
  an MQ heater (about 0.2 A at 5 V each) still drains the battery.
* **Real power saving:** put an **electronic switch** (relay module, or a logic-level MOSFET module) in each
  sensor's supply line and set `PIN_MQ5_POWER`, `PIN_MQ6_POWER` ... in `nano_controller/config.h` to the pin that
  controls it. Set `POWER_ACTIVE_HIGH` to 0 for modules that conduct on LOW. **Never power a sensor straight from
  a Nano pin.**
* **Warm-up:** every time an MQ sensor is switched on, its heater warm-up restarts (`MQ_WARMUP_MS`, 60 s) and
  readings are withheld until it is over. For useful gas readings, leave them on for a few minutes.
* **Soil probe:** switching it off lifts the arm and refuses `deploy`, so the servo stays still in video-only
  mode.
* `DEFAULT_SENSORS_ON` decides whether sensors start on or off after power-up. `HAS_DHT`, `HAS_SOIL`, `HAS_MQ5`,
  `HAS_MQ6`, `HAS_MQ2` say which are fitted (an unfitted sensor cannot be switched and is never reported).
* This sketch drives the motors, the ultrasonic sensor and the sensors from **one** Nano. If you move the
  ultrasonic sensor to a second Nano, the ESP32 would need a second UART link for it; the backend and the app
  only ever see the telemetry, so they do not change.
* Check it with `python -m scripts.nano_console COM5 --check` (switches the first MQ sensor off and on again) and
  `python -m scripts.rover_check http://192.168.4.1` (does the same for the DHT11 over Wi-Fi). Interactively:
  `off mq5`, `on mq5`, `off all`.

## 3. Flashing

Arduino IDE 2 (or `arduino-cli`):

1. **Libraries** (Library Manager): *ArduinoJson* (7.x), *DHT sensor library* + *Adafruit Unified Sensor*. *Servo* is built in.
2. **Nano:** board *Arduino Nano* (processor *ATmega328P*, or *Old Bootloader* for many clones). Open
   `nano_controller/`, check `config.h` (pins, `SOIL_DRY_RAW`/`SOIL_WET_RAW`), upload with D0/D1 disconnected.
3. **ESP32-CAM:** add the board URL `https://espressif.github.io/arduino-esp32/package_esp32_index.json`,
   install *esp32 by Espressif*, choose board *AI Thinker ESP32-CAM*. Edit `esp32cam_rover/config.h`:
   `WIFI_SSID`, `WIFI_PASSWORD`, `BACKEND_URL` (the machine running the backend, e.g. `http://192.168.1.10:8000`)
   and `DEVICE_KEY` (same as `DEVICE_KEY` in `backend/.env`). Upload via the ESP32-CAM-MB board
   (or an FTDI adapter with GPIO0 → GND during reset).
4. Open the serial monitor at 115200 baud: the ESP32 prints its IP (e.g. `Wi-Fi joined …, IP 192.168.1.57`)
   or `Hotspot GAIA-Rover up, IP 192.168.4.1`.

```bash
# command-line equivalent
arduino-cli compile --fqbn arduino:avr:nano firmware/nano_controller
arduino-cli compile --fqbn esp32:esp32:esp32cam firmware/esp32cam_rover
arduino-cli upload  --fqbn esp32:esp32:esp32cam -p COM5 firmware/esp32cam_rover
```

## 4. Network modes (`GAIA_WIFI_MODE` in `esp32cam_rover/config.h`)

| Mode | When to use | Telemetry path |
|---|---|---|
| `GAIA_WIFI_STA_FALLBACK_AP` (default) | rover joins the phone hotspot or farm Wi-Fi; starts its own hotspot if that fails | rover pushes to the backend itself |
| `GAIA_WIFI_AP_ONLY` | no router in the field; the phone joins `GAIA-Rover` (IP `192.168.4.1`) | the app relays readings (Settings → *Relay rover telemetry*) |
| `GAIA_WIFI_AP_STA` | phone on the rover hotspot **and** rover on the farm Wi-Fi | rover pushes to the backend |

Cloud LLM calls happen on the backend, so the backend machine always needs internet access.

## 5. Calibration and bring-up checklist

1. **Soil probe:** print `analogRead(A3)` with the probe in dry air (→ `SOIL_DRY_RAW`) and in a glass of
   water (→ `SOIL_WET_RAW`).
2. **MQ sensors:** new sensors need a 24–48 h burn-in; after each power-on the firmware waits
   `MQ_WARMUP_MS` (60 s) before reporting gas values. Values are raw ADC — a relative index, not ppm.
3. **Arm angles:** adjust `ARM_RETRACTED_DEG` / `ARM_DEPLOYED_DEG` so the probe clears the ground when
   raised and is fully inserted when lowered.
4. **Motors:** raise `MIN_PWM` until all six wheels start turning together at the lowest non-zero speed.
5. **Battery:** compare `battery_v` in `/api/status` with a multimeter; adjust `BATTERY_R1_KOHM`/`R2`.
6. **Safety test:** drive forward from the app and switch off the phone's Wi-Fi — the rover must stop
   within ~0.7 s. Hold a hand 15 cm in front of the ultrasonic sensor — forward drive must be refused.

## 6. Bring-up order with the check tools

Bring the rover up one stage at a time; each stage only adds a few wires, so a failure points at them.
The tools run from `backend/` with the backend's virtual environment and print PASS / WARN / FAIL with
a wiring hint for every check.

| Stage | What is connected | Command |
|---|---|---|
| 0 | New MQ sensors on 5 V only | Start the 24–48 h burn-in as soon as the sensors arrive |
| 1 | Nano on USB + DHT11, MQ sensors, HC-SR04; sensors powered from the 5 V buck with a common GND (three MQ heaters draw ~0.45 A, too much for USB); ESP32 not connected to D0/D1 | `python -m scripts.nano_console COM5 --check` |
| 2 | + servo arm and soil probe (servo powered from the buck) | `python -m scripts.nano_console COM5 --check --arm` |
| 3 | + L298N, motors and battery, **wheels off the ground** | `python -m scripts.nano_console COM5 --check --motors` |
| 4 | ESP32-CAM alone on its programmer, flashed | join the `GAIA-Rover` Wi-Fi (or use the IP printed on the serial monitor), then `python -m scripts.rover_check http://192.168.4.1` — status, camera, snapshot, capture and video stream should pass (the sensor checks need the Nano) |
| 5 | ESP32 ↔ Nano UART with the 1k/2k divider and a common GND | `python -m scripts.rover_check http://192.168.4.1 --arm --drive` |
| 6 | Everything, rover on the ground | the safety test above, then drive from the app |

`python -m scripts.nano_console COM5` without `--check` opens a live console: decoded telemetry once per
second and commands such as `f 150 2` (forward at 150 for 2 s, with heartbeat), `down` / `up` (soil
arm) and `s` (stop). Close the Arduino IDE's serial monitor first; only one program can open the port.
