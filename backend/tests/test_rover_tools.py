"""Hardware bring-up tools, tested without hardware: nano_console against an in-process fake Nano that
speaks the UART protocol, and rover_check against the rover simulator's HTTP API."""

import queue
import socket
import threading
import time

import pytest
import uvicorn

from scripts import nano_console as nc
from scripts.rover_check import run_checks
from scripts.rover_simulator import SimRover, build_app

EXAMPLE = "$T,812,274,610,172,208,191,435,84,3,7860,56*75"  # firmware/PROTOCOL.md


class FakeNano:
    """Mirror of nano_controller.ino's command handling with fast timings."""

    def __init__(self, period=0.05, failsafe=0.3, arm_stage=0.1):
        self.period, self.failsafe, self.arm_stage = period, failsafe, arm_stage
        self.out: queue.Queue[bytes] = queue.Queue()
        self.lock = threading.Lock()
        self.left = self.right = self.arm = self.seq = 0
        self.arm_since = self.last_cmd = time.monotonic()
        self.failsafe_tripped = False
        self.enabled = 0b11111  # all five sensors fitted and on
        self.warm_at = {2: 0.0, 3: 0.0, 4: 0.0}  # bit -> when the MQ heater is considered warm
        self.warm_s = 0.15
        self.running = True
        self.received: list[str] = []
        self.out.put(b"\x00\xf8bootloader noise\n")
        self.out.put(nc.encode("K,BOOT"))
        threading.Thread(target=self._loop, daemon=True).start()

    def write(self, data: bytes) -> int:
        for line in data.decode().splitlines():
            if body := nc.decode(line):
                self.received.append(body)
                self._handle(body.split(","))
        return len(data)

    def readline(self) -> bytes:
        try:
            return self.out.get(timeout=0.05)
        except queue.Empty:
            return b""

    def close(self) -> None:
        self.running = False

    def _reply(self, body: str) -> None:
        self.out.put(nc.encode(body))

    def _handle(self, f: list[str]) -> None:
        with self.lock:
            if f[0] == "D":
                left, right = int(f[1]), int(f[2])
                self.last_cmd, self.failsafe_tripped = time.monotonic(), False
                if (left or right) and self.arm != 0:
                    if self.arm != 4:
                        self.arm, self.arm_since = 4, time.monotonic()
                    self.left = self.right = 0
                    self._reply("E,D,arm")
                else:
                    self.left, self.right = left, right
            elif f[0] == "S":
                self.left = self.right = 0
                self._reply("K,S")
            elif f[0] == "A":
                if f[1] == "1" and (self.left or self.right):
                    self._reply("E,A,moving")
                    return
                if f[1] == "1" and self.arm in (0, 4):
                    self.arm, self.arm_since = 1, time.monotonic()
                elif f[1] == "0" and self.arm != 0:
                    self.arm, self.arm_since = 4, time.monotonic()
                self._reply(f"K,A,{f[1]}")
            elif f[0] == "W":
                names = nc.SWITCHES
                targets = range(len(names)) if f[1] == "all" else ([names.index(f[1])] if f[1] in names else [])
                if not targets:
                    self._reply(f"E,W,{f[1]}")
                    return
                for bit in targets:
                    if f[2] == "1" and not self.enabled >> bit & 1 and bit in self.warm_at:
                        self.warm_at[bit] = time.monotonic() + self.warm_s  # heater warm-up restarts
                    self.enabled = self.enabled | 1 << bit if f[2] == "1" else self.enabled & ~(1 << bit)
                self._reply(f"K,W,{f[1]},{f[2]}")
            elif f[0] == "C":
                self._reply(f"K,C,{f[1]}")
            elif f[0] == "P":
                self._reply("K,P")
            else:
                self._reply(f"E,{f[0]},unknown")

    def _loop(self) -> None:
        while self.running:
            time.sleep(self.period)
            with self.lock:
                now = time.monotonic()
                if (self.left or self.right) and now - self.last_cmd > self.failsafe:
                    self.left = self.right = 0
                    self.failsafe_tripped = True
                if self.arm in (1, 2, 4) and now - self.arm_since > self.arm_stage:
                    self.arm, self.arm_since = {1: 2, 2: 3, 4: 0}[self.arm], now
                moving = bool(self.left or self.right)
                soil_ok = self.arm == 3 and not moving
                en = self.enabled
                on = lambda bit: bool(en >> bit & 1)  # noqa: E731
                warm = 0b00011 | sum(1 << b for b, t in self.warm_at.items() if now >= t)
                soil_ok = soil_ok and on(1)
                flags = moving | self.failsafe_tripped << 2 | on(0) << 3 | 1 << 4 | soil_ok << 5
                temp, hum = (274, 610) if on(0) else (-9999, -9999)
                mq2, mq5, mq6 = (172 if on(4) and warm >> 4 & 1 else -1, 208 if on(2) and warm >> 2 & 1 else -1,
                                 191 if on(3) and warm >> 3 & 1 else -1)
                body = (f"T,{self.seq},{temp},{hum},{mq2},{mq5},{mq6},{435 if soil_ok else -1},84,{self.arm},7860,"
                        f"{flags},31,{en},{warm}")
                self.seq += 1
            self._reply(body)


class SilentPort:
    def write(self, data: bytes) -> int:
        return len(data)

    def readline(self) -> bytes:
        time.sleep(0.05)
        return b""

    def close(self) -> None:
        pass


def test_frames_match_the_protocol_document():
    body = nc.decode(EXAMPLE)
    assert body == EXAMPLE[1:-3]
    assert nc.decode(EXAMPLE[:-2] + "76") is None
    assert nc.encode("P") == b"$P*50\n"
    t = nc.parse_telemetry(body)
    assert (t.temp_c, t.humidity, t.soil_pct, t.distance_cm, t.battery_v) == (27.4, 61.0, 43.5, 84, 7.86)
    assert t.arm_name == "deployed" and t.has(nc.FLAG_SOIL_VALID) and not t.has(nc.FLAG_MOVING)
    missing = nc.parse_telemetry("T,1,-9999,-9999,0,0,0,-1,-1,0,0,0")
    assert (missing.temp_c, missing.soil_pct, missing.distance_cm) == (None, None, None)
    assert "DHT11 --" in missing.describe() and "(warming)" in missing.describe()
    assert nc.parse_telemetry("T,1,2,3") is None
    # An older frame (no masks) means: everything fitted is on, warm-up follows the single heater flag.
    assert t.is_on("mq5") and t.is_fitted("mq2") and t.is_warm("mq6")
    # The extended frame carries installed / enabled / warm masks.
    sw = nc.parse_telemetry("T,9,274,610,-1,-1,191,-1,84,0,7860,24,15,11,7")
    assert sw.is_fitted("mq6") and not sw.is_fitted("mq2")
    assert sw.is_on("dht11") and sw.is_on("mq6") and not sw.is_on("mq5")  # enabled mask 11 = dht11, soil, mq6
    assert sw.is_warm("dht11") and not sw.is_warm("mq6")  # warm mask 7 = dht11, soil, mq5
    assert "MQ5 off" in sw.describe() and "MQ6 191 (warming)" in sw.describe() and "MQ2" not in sw.describe()
    # The frames printed in firmware/PROTOCOL.md carry valid checksums and decode as documented.
    full = nc.parse_telemetry(nc.decode("$T,812,274,610,-1,208,191,435,84,3,7860,56,15,15,15*75"))
    assert full.is_on("mq5") and full.is_on("mq6") and not full.is_fitted("mq2") and full.mq5 == 208
    gas_off = nc.parse_telemetry(nc.decode("$T,813,274,610,-1,-1,-1,435,84,3,7860,56,15,3,3*77"))
    assert gas_off.enabled == 3 and not gas_off.is_on("mq5") and gas_off.mq5 == -1
    assert nc.encode("W,mq5,0") == b"$W,mq5,0*4E\n" and nc.encode("W,all,0") == b"$W,all,0*06\n"


def test_nano_check_passes_against_the_fake_nano():
    fake = FakeNano()
    link = nc.NanoLink(fake)
    try:
        results = nc.run_check(link, motors=True, arm=True, report=lambda _: None, window_s=0.5, pulse_s=0.3,
                               arm_timeout=3.0)
    finally:
        link.close()
    bad = [r for r in results if r.status in ("WARN", "FAIL")]
    assert not bad, bad
    names = {r.name for r in results if r.status == "PASS"}
    assert {"link", "ping", "telemetry", "DHT11", "arm deploy", "probe interlock", "arm retract",
            "drive forward", "spin right", "failsafe", "sensor switches"} <= names
    assert {"C,OBS,20", "C,MAX,230", "A,1", "A,0", "D,150,150", "D,-150,150", "S"} <= set(fake.received)
    assert {"W,mq5,0", "W,mq5,1"} <= set(fake.received)  # the switch self-check turned MQ5 off and on again
    assert fake.enabled == 0b11111  # ... and restored the original state
    assert fake.received[-1] == "S"  # close() always stops the motors


def test_nano_check_explains_a_silent_port():
    link = nc.NanoLink(SilentPort())
    try:
        results = nc.run_check(link, report=lambda _: None, boot_timeout=0.3)
    finally:
        link.close()
    assert [(r.status, r.name) for r in results] == [("FAIL", "link")]
    assert "57600" in results[0].hint


@pytest.fixture
def simulator():
    rover = SimRover("rover-test", None)
    app = build_app(rover, None, "test-key", sample_s=60, push_s=60)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=10)


def test_rover_check_passes_against_the_simulator(simulator):
    results = run_checks(simulator, f"{simulator}/stream", drive=True, arm=True, stream_seconds=1.0, pulse_s=0.3)
    assert not [r for r in results if r.status == "FAIL"], results
    passed = {r.name for r in results if r.status == "PASS"}
    assert {"status", "nano link", "camera", "DHT11", "snapshot", "capture", "arm deploy", "arm retract",
            "watchdog"} <= passed
    # timing-dependent on a busy machine (frame rate, random obstacles): present, but may only warn
    assert {"video stream", "drive forward", "spin left"} <= {r.name for r in results}


def test_simulator_sensor_switches_over_http(simulator):
    import httpx

    http = httpx.Client(base_url=simulator, timeout=5)
    state = http.get("/api/sensors").json()
    assert state["switches"] == {"dht11": True, "soil_moisture": True, "mq5": True, "mq6": True}

    reply = http.post("/api/sensors", json={"dht11": False, "mq6": False}).json()
    assert reply["ok"] and reply["switches"]["dht11"] is False and reply["switches"]["mq6"] is False
    t = http.get("/api/telemetry").json()
    assert "temperature" not in t["values"] and "humidity" not in t["values"] and "mq6" not in t["values"]
    assert t["state"]["sensor_switches"]["dht11"] is False
    assert http.get("/api/status").json()["sensors"]["switches"]["mq6"] is False

    http.post("/api/sensors", json={"mq6": True})  # powering an MQ sensor on restarts its warm-up
    assert "mq6" in http.get("/api/sensors").json()["warming"]
    assert "mq6" not in http.get("/api/telemetry").json()["values"]

    assert http.post("/api/sensors", json={"all": False}).json()["switches"] == {
        "dht11": False, "soil_moisture": False, "mq5": False, "mq6": False}  # video-only mode
    assert http.post("/api/sensors", json={"bogus": True}).status_code == 400
    assert http.post("/api/sensors", json={}).status_code == 400


def test_rover_check_explains_an_unreachable_rover():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens here any more
    results = run_checks(f"http://127.0.0.1:{port}")
    assert [(r.status, r.name) for r in results] == [("FAIL", "status")]
    assert "GAIA-Rover" in results[0].hint
