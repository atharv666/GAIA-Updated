"""Rover simulator: speaks the same HTTP API as the ESP32-CAM firmware (firmware/PROTOCOL.md).

    python -m scripts.rover_simulator --port 8090 --backend http://localhost:8000

Point the backend (backend/.env) and the app at it:
    ROVER_BASE_URL=http://localhost:8090
    ROVER_STREAM_URL=http://localhost:8090/stream      (the real rover streams on :81/stream)

Emulated behaviour mirrors the firmware: drive failsafe (stops if no command
for 600 ms), obstacle stop, soil-probe arm state machine (the soil reading is
only valid once the arm is deployed and settled), telemetry batches pushed to
the backend with uptime-based timestamps, MJPEG stream and high-res capture.
Use --capture-dir to serve real leaf photos from /capture for VLM testing.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import math
import random
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import Response, StreamingResponse  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from scripts.farm_model import FarmModel  # noqa: E402

log = logging.getLogger("rover-sim")

FAILSAFE_S = 0.6
ARM_MOVE_S = 1.5
ARM_SETTLE_S = 2.0
OBSTACLE_STOP_CM = 20
MQ_WARMUP_S = 6.0  # the real heaters need about a minute; the simulator keeps demos snappy
SWITCHES = ("dht11", "soil_moisture", "mq5", "mq6")  # the sensors fitted to the simulated rover
SWITCH_VALUES = {"dht11": ("temperature", "humidity"), "soil_moisture": ("soil_moisture",),
                 "mq5": ("mq5",), "mq6": ("mq6",)}


class SimRover:
    def __init__(self, device_id: str, capture_dir: Path | None):
        self.device_id = device_id
        self.capture_dir = capture_dir
        self.boot = time.monotonic()
        self.lock = threading.Lock()
        self.left = self.right = 0
        self.last_cmd = 0.0
        self.arm = "retracted"
        self.arm_since = time.monotonic()
        self.pending_deploy = False
        self.distance = 150.0
        self.battery = 8.2
        self.position = 0.0
        self.heading = 0.0
        self.model = FarmModel(seed=random.randint(1, 10_000), spike_probability=0.004)
        self.backend_ok: bool | None = None
        self.queue: list[dict] = []
        self.switches = {k: True for k in SWITCHES}
        self.warm_until = {k: self.boot + MQ_WARMUP_S for k in ("mq5", "mq6")}
        self.push_now = False

    @property
    def uptime_ms(self) -> int:
        return int((time.monotonic() - self.boot) * 1000)

    @property
    def moving(self) -> bool:
        return self.left != 0 or self.right != 0

    def tick(self) -> None:
        now = time.monotonic()
        with self.lock:
            if self.moving and now - self.last_cmd > FAILSAFE_S:
                self.left = self.right = 0
            if self.arm in ("deploying", "retracting") and now - self.arm_since > ARM_MOVE_S:
                self.arm = "settling" if self.arm == "deploying" else "retracted"
                self.arm_since = now
            elif self.arm == "settling" and now - self.arm_since > ARM_SETTLE_S:
                self.arm, self.arm_since = "deployed", now
            speed = (self.left + self.right) / 2
            self.position += speed / 255 * 0.05
            self.heading += (self.left - self.right) / 255 * 0.03
            self.distance = max(8.0, min(300.0, self.distance + random.gauss(0, 3) - speed / 255 * 2))
            if self.distance < 25 and random.random() < 0.1:
                self.distance = random.uniform(80, 250)  # obstacle passed
            if self.left > 0 and self.right > 0 and self.distance < OBSTACLE_STOP_CM:
                self.left = self.right = 0
            self.battery = max(6.4, self.battery - (0.00002 + 0.00008 * self.moving))

    def drive(self, left: int, right: int) -> dict:
        with self.lock:
            self.last_cmd = time.monotonic()
            if (left or right) and self.arm != "retracted":
                if self.arm != "retracting":
                    self.arm, self.arm_since = "retracting", time.monotonic()
                self.left = self.right = 0
                return {"ok": True, "held": "soil probe retracting"}
            if left > 0 and right > 0 and self.distance < OBSTACLE_STOP_CM:
                self.left = self.right = 0
                return {"ok": False, "blocked": "obstacle", "distance_cm": round(self.distance)}
            self.left, self.right = max(-255, min(255, left)), max(-255, min(255, right))
            return {"ok": True, "left": self.left, "right": self.right}

    def set_arm(self, action: str) -> dict:
        with self.lock:
            if action == "deploy":
                if self.moving:
                    return {"ok": False, "error": "stop the rover before lowering the probe"}
                if self.arm in ("retracted", "retracting"):
                    self.arm, self.arm_since = "deploying", time.monotonic()
            else:
                if self.arm != "retracted":
                    self.arm, self.arm_since = "retracting", time.monotonic()
            return {"ok": True, "arm": self.arm}

    def warming(self) -> list[str]:
        now = time.monotonic()
        return [k for k, until in self.warm_until.items() if self.switches[k] and now < until]

    def set_switches(self, requested: dict[str, bool]) -> dict:
        """Switch sensors on or off. Powering an MQ sensor on restarts its heater warm-up."""
        unknown = [k for k in requested if k not in self.switches]
        if unknown:
            return {"ok": False, "error": f"unknown sensor(s): {', '.join(unknown)}", "sensors": self.sensors_state()}
        with self.lock:
            for key, on in requested.items():
                if on and not self.switches[key] and key in self.warm_until:
                    self.warm_until[key] = time.monotonic() + MQ_WARMUP_S
                self.switches[key] = bool(on)
            self.push_now = True  # tell the backend straight away, like the firmware does
        return {"ok": True, **self.sensors_state()}

    def sensors_state(self) -> dict:
        return {"switches": dict(self.switches), "warming": self.warming(), "installed": list(SWITCHES)}

    def telemetry(self) -> dict:
        values = self.model.sample(datetime.now().astimezone())
        values.pop("mq2", None)  # no MQ2 on this rover
        soil_valid = self.arm == "deployed" and not self.moving
        if not soil_valid:
            values.pop("soil_moisture")
        hidden = {k for k in SWITCHES if not self.switches[k]} | set(self.warming())
        for key in hidden:
            for name in SWITCH_VALUES[key]:
                values.pop(name, None)
        soil_valid = soil_valid and "soil_moisture" in values
        values["distance_cm"] = round(self.distance)
        values["battery_v"] = round(self.battery, 2)
        return {
            "device_id": self.device_id,
            "uptime_ms": self.uptime_ms,
            "values": values,
            "state": {
                "moving": self.moving,
                "arm_deployed": self.arm == "deployed",
                "soil_valid": soil_valid,
                "obstacle": self.distance < OBSTACLE_STOP_CM,
                "failsafe": False,
                "sensor_switches": dict(self.switches),
                "warming": self.warming(),
            },
        }

    def frame(self, w: int, h: int) -> bytes:
        img = Image.new("RGB", (w, h), (27, 46, 40))
        d = ImageDraw.Draw(img)
        horizon = int(h * 0.38)
        for y in range(horizon):
            c = int(150 + 80 * y / horizon)
            d.line([(0, y), (w, y)], fill=(c - 40, c, 255))
        d.rectangle([0, horizon, w, h], fill=(96, 72, 48))
        shift = (self.position * 120) % 60
        for row in range(-6, 7):
            x_far = w / 2 + row * w * 0.05 + self.heading * 40
            x_near = w / 2 + row * w * 0.28 + self.heading * 160
            d.line([(x_far, horizon), (x_near, h)], fill=(70, 52, 34), width=max(2, w // 200))
            for k in range(8):
                t = ((k * 60 + shift) % 480) / 480
                y = horizon + (h - horizon) * t
                x = x_far + (x_near - x_far) * t + w * 0.07 * t
                r = 3 + 22 * t * w / 640
                d.ellipse([x - r, y - r * 0.7, x + r, y + r * 0.7], fill=(63 + int(40 * math.sin(k)), 125, 60))
        d.rectangle([10, 10, 230, 58], fill=(0, 0, 0))
        d.text((18, 16), f"GAIA ROVER SIM  {datetime.now():%H:%M:%S}", fill=(197, 229, 138))
        d.text((18, 34), f"L{self.left:+4d} R{self.right:+4d}  arm:{self.arm}  {self.distance:.0f}cm", fill=(255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=80)
        return buf.getvalue()

    def capture(self) -> bytes:
        if self.capture_dir:
            photos = [p for p in self.capture_dir.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
            if photos:
                return random.choice(photos).read_bytes()
        return self.frame(1600, 1200)


def build_app(rover: SimRover, backend: str | None, device_key: str, sample_s: float, push_s: float) -> FastAPI:
    async def physics():
        while True:
            rover.tick()
            await asyncio.sleep(0.05)

    async def telemetry_pusher():
        last_push = time.monotonic()
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                await asyncio.sleep(min(sample_s, 0.5) if rover.push_now else sample_s)
                t = rover.telemetry()
                rover.queue.append({"uptime_ms": t["uptime_ms"], "values": t["values"], "state": t["state"]})
                rover.queue = rover.queue[-2000:]
                due = time.monotonic() - last_push >= push_s
                if not backend or not (due or rover.push_now):
                    continue
                last_push = time.monotonic()
                rover.push_now = False
                batch = {"device_id": rover.device_id, "sent_uptime_ms": rover.uptime_ms, "readings": rover.queue,
                         "sensor_switches": dict(rover.switches)}
                try:
                    r = await client.post(f"{backend}/api/v1/telemetry", json=batch, headers={"X-Device-Key": device_key})
                    r.raise_for_status()
                    rover.backend_ok = True
                    log.info("pushed %d readings -> %s", len(batch["readings"]), r.json())
                    rover.queue = []
                except Exception as exc:  # keep queue, retry next push
                    rover.backend_ok = False
                    log.warning("telemetry push failed (%s); %d queued", exc, len(rover.queue))

    @asynccontextmanager
    async def lifespan(_app):
        tasks = [asyncio.create_task(physics()), asyncio.create_task(telemetry_pusher())]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(title="GAIA rover simulator", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/api/status")
    def status():
        t = rover.telemetry()
        return {
            "device_id": rover.device_id,
            "firmware": "simulator-1.0",
            "uptime_ms": rover.uptime_ms,
            "wifi": {"mode": "sim", "ssid": "localhost", "rssi": -42, "ip": "127.0.0.1"},
            "nano": {"connected": True, "last_seen_ms": 20},
            "camera": {"ok": True},
            "drive": {"left": rover.left, "right": rover.right},
            "arm": rover.arm,
            "sensors": rover.sensors_state(),
            "obstacle_cm": t["values"]["distance_cm"],
            "battery_v": t["values"]["battery_v"],
            "backend": {"url": backend, "last_push_ok": rover.backend_ok, "queued": len(rover.queue)},
        }

    @app.get("/api/telemetry")
    def telemetry():
        return rover.telemetry()

    @app.post("/api/drive")
    def drive(cmd: dict):
        if "left" in cmd or "right" in cmd:
            left, right = int(cmd.get("left", 0)), int(cmd.get("right", 0))
        elif "throttle" in cmd or "steer" in cmd:
            th, st = float(cmd.get("throttle", 0)), float(cmd.get("steer", 0))
            left, right = int(255 * max(-1, min(1, th + st))), int(255 * max(-1, min(1, th - st)))
        elif "dir" in cmd:
            s = int(cmd.get("speed", 180))
            left, right = {"forward": (s, s), "reverse": (-s, -s), "left": (-s, s), "right": (s, -s),
                           "stop": (0, 0)}.get(cmd["dir"], (0, 0))
        else:
            raise HTTPException(400, "expected left/right, throttle/steer or dir")
        return rover.drive(left, right)

    @app.post("/api/stop")
    def stop():
        return rover.drive(0, 0)

    @app.post("/api/arm")
    def arm(cmd: dict):
        if cmd.get("action") not in ("deploy", "retract"):
            raise HTTPException(400, "action must be deploy or retract")
        return rover.set_arm(cmd["action"])

    @app.get("/api/sensors")
    def sensors():
        return rover.sensors_state()

    @app.post("/api/sensors")
    def set_sensors(cmd: dict):
        """{"mq5": false} or {"switches": {...}} or {"all": false} (video-only mode)."""
        if "all" in cmd:
            requested = {k: bool(cmd["all"]) for k in SWITCHES}
        elif isinstance(cmd.get("switches"), dict):
            requested = {k: bool(v) for k, v in cmd["switches"].items()}
        else:
            requested = {k: bool(v) for k, v in cmd.items() if isinstance(v, bool)}
        if not requested:
            raise HTTPException(400, "expected {sensor: true|false}, {switches: {...}} or {all: true|false}")
        reply = rover.set_switches(requested)
        if not reply["ok"]:
            raise HTTPException(400, reply["error"])
        return reply

    @app.post("/api/config")
    def config(cmd: dict):
        return {"ok": True, "applied": {k: cmd[k] for k in ("obstacle_cm", "max_pwm") if k in cmd}}

    @app.get("/capture")
    def capture():
        return Response(rover.capture(), media_type="image/jpeg")

    @app.get("/snapshot")
    def snapshot():
        return Response(rover.frame(640, 480), media_type="image/jpeg")

    @app.get("/stream")
    async def stream():
        async def frames():
            while True:
                jpg = await asyncio.to_thread(rover.frame, 640, 480)
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n"
                await asyncio.sleep(0.12)

        return StreamingResponse(frames(), media_type="multipart/x-mixed-replace; boundary=frame")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--backend", default="http://localhost:8000", help="GAIA backend URL ('' to disable push)")
    parser.add_argument("--device-key", default="gaia-rover-dev-key")
    parser.add_argument("--device-id", default="rover-01")
    parser.add_argument("--sample-seconds", type=float, default=5.0)
    parser.add_argument("--push-seconds", type=float, default=15.0)
    parser.add_argument("--capture-dir", type=Path, help="folder of real crop photos served by /capture")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    rover = SimRover(args.device_id, args.capture_dir)
    app = build_app(rover, args.backend.rstrip("/") or None, args.device_key, args.sample_seconds, args.push_seconds)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
