"""Step-by-step check of a GAIA rover (the real ESP32-CAM or the simulator) over its HTTP API.

    python -m scripts.rover_check http://192.168.4.1              # read-only checks (rover hotspot)
    python -m scripts.rover_check http://192.168.4.1 --drive      # + drive pulses and the watchdog test
    python -m scripts.rover_check http://192.168.4.1 --arm        # + lower and raise the soil probe
    python -m scripts.rover_check http://localhost:8090 --stream-url http://localhost:8090/stream   # simulator

Run --drive only with the wheels OFF the ground. Every check prints PASS, WARN or FAIL with a hint;
the exit code is 1 if anything failed. The API is documented in firmware/PROTOCOL.md section 1.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from PIL import Image

ARM_STEPS = ("deploying", "settling", "deployed")


@dataclass
class Result:
    status: str  # PASS | WARN | FAIL | INFO
    name: str
    detail: str
    hint: str = ""


class Checker:
    def __init__(self, base_url: str, stream_url: str | None = None, timeout: float = 5.0):
        self.base = base_url.rstrip("/")
        parts = urlsplit(self.base)
        self.stream_url = stream_url or f"{parts.scheme}://{parts.hostname}:81/stream"
        self.http = httpx.Client(timeout=timeout, trust_env=False)  # the rover is always local: never via a proxy
        self.results: list[Result] = []

    def add(self, status: str, name: str, detail: str, hint: str = "") -> Result:
        r = Result(status, name, detail, hint)
        self.results.append(r)
        print(f"[{status}] {name}: {detail}")
        if hint and status in ("WARN", "FAIL"):
            print(f"       hint: {hint}")
        return r

    def get_json(self, path: str) -> dict:
        r = self.http.get(self.base + path)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, body: dict | None = None) -> dict:
        r = self.http.post(self.base + path, json=body or {})
        r.raise_for_status()
        return r.json()

    # ------------------------------------------------------------ read-only checks

    def check_status(self) -> dict | None:
        t0 = time.perf_counter()
        try:
            s = self.get_json("/api/status")
        except (httpx.HTTPError, ValueError) as exc:
            self.add("FAIL", "status", f"GET /api/status failed: {exc}",
                     "Is the PC on the rover's Wi-Fi (GAIA-Rover hotspot) or the same LAN? Check the IP the "
                     "ESP32 prints on its serial monitor at boot.")
            return None
        ms = (time.perf_counter() - t0) * 1000
        wifi = s.get("wifi", {})
        self.add("PASS", "status", f"firmware {s.get('firmware')} on {s.get('device_id')}, up "
                 f"{s.get('uptime_ms', 0) / 1000:.0f} s, Wi-Fi {wifi.get('mode')} {wifi.get('ip')} ({ms:.0f} ms)")
        rssi = wifi.get("rssi")
        if isinstance(rssi, (int, float)) and rssi < -75:
            self.add("WARN", "wifi signal", f"RSSI {rssi} dBm",
                     "Weak signal: move closer, or fit an external antenna (move the ESP32-CAM's 0-ohm link).")
        nano = s.get("nano", {})
        if nano.get("connected"):
            self.add("PASS", "nano link", f"telemetry seen {nano.get('last_seen_ms', '?')} ms ago")
        else:
            self.add("FAIL", "nano link", "no telemetry from the Arduino Nano",
                     "Check ESP32 GPIO14 -> Nano D0, Nano D1 -> 1k/2k divider -> GPIO13, a common GND, and that "
                     "the Nano is powered. Test the Nano alone first: python -m scripts.nano_console COMx --check")
        if s.get("camera", {}).get("ok"):
            self.add("PASS", "camera", "initialised")
        else:
            self.add("FAIL", "camera", "camera did not initialise",
                     "Reseat the OV2640 ribbon cable, enable PSRAM in the board settings and use a solid "
                     "5 V / 2 A supply.")
        if s.get("battery_v") is None:
            self.add("WARN", "battery", "no battery reading", "Wire the 10k/4.7k divider from the battery to Nano A6.")
        else:
            self.add("INFO", "battery", f"{s['battery_v']:.2f} V (compare with a multimeter; tune "
                     "BATTERY_R1_KOHM / BATTERY_R2_KOHM)")
        backend = s.get("backend", {})
        if backend.get("url") and backend.get("last_push_ok") is False:
            queued = backend.get("queued", 0)
            self.add("WARN", "backend push", f"cannot reach {backend['url']} ({queued} readings queued)",
                     "In hotspot mode the rover has no route to the backend: enable 'Relay rover telemetry' in the "
                     "app, or put the rover on your Wi-Fi (GAIA_WIFI_MODE) and set BACKEND_URL to the PC's LAN IP.")
        return s

    def check_telemetry(self) -> None:
        try:
            first = self.get_json("/api/telemetry")
            time.sleep(1.2)
            t = self.get_json("/api/telemetry")
        except (httpx.HTTPError, ValueError) as exc:
            self.add("FAIL", "telemetry", f"GET /api/telemetry failed: {exc}")
            return
        if t.get("uptime_ms", 0) <= first.get("uptime_ms", 0):
            self.add("WARN", "telemetry", "uptime did not advance between two reads", "The ESP32 may be rebooting "
                     "(brown-out): check the 5 V supply and wiring.")
        v = t.get("values", {})
        if "temperature" in v and "humidity" in v:
            ok = -10 <= v["temperature"] <= 60 and 5 <= v["humidity"] <= 100
            self.add("PASS" if ok else "WARN", "DHT11", f"{v['temperature']} C, {v['humidity']} % RH",
                     "" if ok else "Values out of range: check the sensor and the pull-up on D2.")
        else:
            self.add("FAIL", "DHT11", "no temperature/humidity",
                     "Check DHT11 data on D2 (10k pull-up to 5 V) and power.")
        mq = {k: v[k] for k in ("mq2", "mq5", "mq6") if k in v}
        if not mq:
            self.add("INFO", "MQ sensors", "no gas values yet (60 s heater warm-up after power-on); re-run in a minute")
        for k, val in mq.items():
            if val <= 5 or val >= 1018:
                self.add("WARN", k.upper(), f"raw {val} (stuck at a rail)", f"Check the {k.upper()} module's AO wire "
                         "(A0/A1/A2) and its 5 V heater supply.")
            else:
                self.add("PASS", k.upper(), f"raw {val} (relative index, not ppm)")
        if "distance_cm" in v:
            self.add("PASS", "ultrasonic", f"{v['distance_cm']} cm")
        else:
            self.add("WARN", "ultrasonic", "no echo", "Check TRIG D9 / ECHO D10 and 5 V; nothing within ~4 m also "
                     "gives no echo.")

    def check_switches(self, settle_s: float = 1.5) -> None:
        """Switch the DHT11 off and on again (restoring its state) and watch the telemetry follow."""
        try:
            state = self.get_json("/api/sensors")
        except (httpx.HTTPError, ValueError):
            self.add("WARN", "sensor switches", "GET /api/sensors failed",
                     "Re-flash esp32cam_rover: this firmware has no per-sensor switches yet.")
            return
        switches = state.get("switches", {})
        if "dht11" not in switches:
            self.add("WARN", "sensor switches", "dht11 not reported", "Check the Nano firmware version.")
            return
        was_on = switches["dht11"]
        try:
            self.post("/api/sensors", {"dht11": False})
            time.sleep(settle_s)
            off = self.get_json("/api/telemetry").get("values", {})
            self.post("/api/sensors", {"dht11": True})
            time.sleep(settle_s)
            on = self.get_json("/api/telemetry").get("values", {})
        finally:
            self.post("/api/sensors", {"dht11": was_on})
        ok = "temperature" not in off and "temperature" in on
        self.add("PASS" if ok else "FAIL", "sensor switches",
                 "DHT11 off -> readings stop, on -> readings return" if ok
                 else f"readings did not follow the switch (off: {sorted(off)}, on: {sorted(on)})",
                 "" if ok else "Check the W command path (ESP32 -> Nano) and the enable mask in the telemetry frame.")

    def check_jpeg(self, path: str, name: str, min_width: int) -> None:
        t0 = time.perf_counter()
        try:
            r = self.http.get(self.base + path, timeout=15)
            r.raise_for_status()
            with Image.open(io.BytesIO(r.content)) as img:
                img.verify()
                size = img.size
        except Exception as exc:  # any network or decode error is a failed check
            self.add("FAIL", name, f"GET {path} failed: {exc}",
                     "Camera or PSRAM problem: reseat the ribbon cable and check the 5 V supply.")
            return
        ms = (time.perf_counter() - t0) * 1000
        status = "PASS" if size[0] >= min_width else "WARN"
        self.add(status, name, f"{size[0]}x{size[1]} JPEG, {len(r.content) // 1024} KB in {ms:.0f} ms",
                 "" if status == "PASS" else "Lower resolution than expected: is PSRAM enabled?")

    def check_stream(self, seconds: float = 3.0) -> None:
        frames, buf = 0, b""
        t0 = time.perf_counter()
        try:
            with self.http.stream("GET", self.stream_url, timeout=httpx.Timeout(5.0)) as r:
                r.raise_for_status()
                for chunk in r.iter_bytes():
                    buf += chunk
                    while (soi := buf.find(b"\xff\xd8")) != -1 and (eoi := buf.find(b"\xff\xd9", soi + 2)) != -1:
                        frames += 1
                        buf = buf[eoi + 2:]
                    buf = buf[-500_000:]
                    if time.perf_counter() - t0 >= seconds:
                        break
        except httpx.HTTPError as exc:
            self.add("FAIL", "video stream", f"{self.stream_url}: {exc}",
                     "The MJPEG server runs on port 81 of the ESP32; pass --stream-url for other setups.")
            return
        fps = frames / max(time.perf_counter() - t0, 1e-6)
        if frames == 0:
            self.add("FAIL", "video stream", "no frames received", "Check the camera; restart the ESP32.")
        else:
            self.add("PASS" if fps >= 5 else "WARN", "video stream", f"{frames} frames, {fps:.1f} fps",
                     "Low frame rate: weak Wi-Fi, or lower STREAM_FRAMESIZE in esp32cam_rover/config.h.")

    # ------------------------------------------------------------ actuator checks

    def _drive_pulse(self, name: str, body: dict, seconds: float, period: float = 0.12) -> None:
        end = time.monotonic() + seconds
        reply, seen = {}, (0, 0)
        while time.monotonic() < end:
            reply = self.post("/api/drive", body)
            if not reply.get("ok") or reply.get("held"):
                break
            drive = self.get_json("/api/status").get("drive", {})
            seen = (drive.get("left", 0), drive.get("right", 0))
            time.sleep(period)
        self.post("/api/stop")
        if reply.get("blocked"):
            self.add("WARN", name, f"refused: obstacle at {reply.get('distance_cm')} cm",
                     "Clear the space in front of the ultrasonic sensor (or lower obstacle_cm).")
        elif reply.get("held"):
            self.add("WARN", name, "held while the soil probe retracts; run the check again")
        elif seen != (0, 0):
            self.add("PASS", name, f"motors commanded L{seen[0]:+d} R{seen[1]:+d}; watch that all six wheels turn "
                     "the right way (swap wires or set INVERT_LEFT/RIGHT if not)")
        else:
            self.add("FAIL", name, f"drive accepted but the status never showed motion ({reply})")

    def check_drive(self, pulse_s: float = 1.0) -> None:
        print("Driving: the wheels must be OFF the ground.")
        try:
            self.post("/api/arm", {"action": "retract"})
            for name, direction in (("drive forward", "forward"), ("drive reverse", "reverse"),
                                    ("spin left", "left"), ("spin right", "right")):
                self._drive_pulse(name, {"dir": direction, "speed": 150}, pulse_s)
            # Watchdog: one command, then silence. The ESP32 must stop within 0.5 s (the Nano within 0.7 s).
            first = self.post("/api/drive", {"dir": "reverse", "speed": 120})
            if first.get("ok") and not first.get("held"):
                time.sleep(1.2)
                drive = self.get_json("/api/status").get("drive", {})
                if (drive.get("left", 0), drive.get("right", 0)) == (0, 0):
                    self.add("PASS", "watchdog", "motors stopped without a heartbeat (Wi-Fi loss protection works)")
                else:
                    self.add("FAIL", "watchdog", f"motors still commanded after 1.2 s: {drive}",
                             "Do not run the rover until DRIVE_TIMEOUT_MS / FAILSAFE_MS work.")
            else:
                self.add("WARN", "watchdog", f"could not start the test: {first}")
        finally:
            self.post("/api/stop")

    def check_arm(self, deploy_timeout: float = 12.0, retract_timeout: float = 8.0) -> None:
        self.post("/api/stop")
        reply = self.post("/api/arm", {"action": "deploy"})
        if not reply.get("ok"):
            self.add("FAIL", "arm deploy", f"refused: {reply}")
            return
        states = self._wait_arm("deployed", deploy_timeout)
        if states[-1] != "deployed":
            self.add("FAIL", "arm deploy", f"states {' -> '.join(states)}",
                     "Check the servo on D3 (powered from the 5 V buck, not the Nano's 5 V pin).")
        else:
            soil = self.get_json("/api/telemetry").get("values", {}).get("soil_moisture")
            if soil is None:
                self.add("WARN", "soil probe", "arm deployed but no soil reading",
                         "Check the probe on A3; calibrate SOIL_DRY_RAW / SOIL_WET_RAW.")
            else:
                self.add("PASS", "arm deploy", f"{' -> '.join(states)}; soil moisture {soil} %")
        self.post("/api/arm", {"action": "retract"})
        states = self._wait_arm("retracted", retract_timeout)
        self.add("PASS" if states[-1] == "retracted" else "FAIL", "arm retract", " -> ".join(states))

    def _wait_arm(self, target: str, timeout: float) -> list[str]:
        states: list[str] = []
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            arm = self.get_json("/api/status").get("arm", "unknown")
            if not states or states[-1] != arm:
                states.append(arm)
            if arm == target:
                break
            time.sleep(0.2)
        return states or ["unknown"]


def run_checks(base_url: str, stream_url: str | None = None, *, drive: bool = False, arm: bool = False,
               stream_seconds: float = 3.0, pulse_s: float = 1.0) -> list[Result]:
    c = Checker(base_url, stream_url)
    try:
        if c.check_status() is None:
            return c.results
        c.check_telemetry()
        c.check_switches()
        c.check_jpeg("/snapshot", "snapshot", 320)
        c.check_jpeg("/capture", "capture", 800)
        c.check_stream(stream_seconds)
        if arm:
            c.check_arm()
        if drive:
            c.check_drive(pulse_s)
    finally:
        c.http.close()
    return c.results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url", nargs="?", default="http://192.168.4.1", help="rover base URL (port 80)")
    parser.add_argument("--stream-url", help="MJPEG URL (default: same host, port 81, /stream)")
    parser.add_argument("--drive", action="store_true",
                        help="also test driving and the watchdog (wheels off the ground!)")
    parser.add_argument("--arm", action="store_true", help="also lower and raise the soil probe")
    args = parser.parse_args()
    results = run_checks(args.url, args.stream_url, drive=args.drive, arm=args.arm)
    counts = {s: sum(r.status == s for r in results) for s in ("PASS", "WARN", "FAIL")}
    print(f"\n{counts['PASS']} passed, {counts['WARN']} warnings, {counts['FAIL']} failed")
    sys.exit(1 if counts["FAIL"] else 0)


if __name__ == "__main__":
    main()
