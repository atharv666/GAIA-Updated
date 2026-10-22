"""USB bring-up console for the rover's Arduino Nano (motors + sensors), without the ESP32-CAM.

Plug the Nano into the PC over USB with its D0/D1 wires to the ESP32 disconnected, then:

    python -m scripts.nano_console --list                   # find the Nano's COM port
    python -m scripts.nano_console COM5                     # live decoded telemetry + manual commands
    python -m scripts.nano_console COM5 --check             # automated link and sensor checks
    python -m scripts.nano_console COM5 --check --arm       # + lower/raise the soil probe, read the soil sensor
    python -m scripts.nano_console COM5 --check --motors    # + drive pulses and the failsafe test

--motors turns the wheels: lift the rover so they are OFF the ground. The console speaks the same UART
protocol as the ESP32 (firmware/PROTOCOL.md section 2): "$BODY*CS" lines at 57600 baud.
Needs pyserial (in requirements-local.txt).
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

BAUD = 57600
ARM_NAMES = ("retracted", "deploying", "settling", "deployed", "retracting")
ARM_RETRACTED, ARM_DEPLOYED = 0, 3
FLAG_MOVING, FLAG_OBSTACLE, FLAG_FAILSAFE, FLAG_DHT_OK, FLAG_MQ_WARM, FLAG_SOIL_VALID = (1 << i for i in range(6))
DEFAULT_OBSTACLE_CM, DEFAULT_MAX_PWM = 20, 230  # nano_controller/config.h
# Sensor switches: bit index in the inst / en / warm masks of the telemetry frame (firmware/PROTOCOL.md).
SWITCHES = ("dht11", "soil", "mq5", "mq6", "mq2")


# ---------------------------------------------------------------- frames

def checksum(body: str) -> str:
    cs = 0
    for ch in body.encode("ascii", "replace"):
        cs ^= ch
    return f"{cs:02X}"


def encode(body: str) -> bytes:
    return f"${body}*{checksum(body)}\n".encode("ascii")


def decode(line: str) -> str | None:
    """Return the body of a valid "$BODY*CS" frame, or None (noise, bad checksum)."""
    line = line.strip()
    if not line.startswith("$") or "*" not in line:
        return None
    body, _, cs = line[1:].rpartition("*")
    return body if cs.upper() == checksum(body) else None


@dataclass
class Telemetry:
    seq: int
    temp_c: float | None
    humidity: float | None
    mq2: int
    mq5: int
    mq6: int
    soil_pct: float | None
    distance_cm: int | None
    arm: int
    battery_v: float
    flags: int
    installed: int = 0b01111  # which sensors are fitted (bit per SWITCHES entry)
    enabled: int = 0b01111  # which are switched on
    warm: int = 0b01111  # which have finished warming up (MQ heaters)

    def has(self, flag: int) -> bool:
        return bool(self.flags & flag)

    def is_fitted(self, name: str) -> bool:
        return bool(self.installed >> SWITCHES.index(name) & 1)

    def is_on(self, name: str) -> bool:
        return bool(self.enabled >> SWITCHES.index(name) & 1)

    def is_warm(self, name: str) -> bool:
        return bool(self.warm >> SWITCHES.index(name) & 1)

    def _mq(self, name: str, value: int) -> str:
        if not self.is_on(name):
            return f"{name.upper()} off"
        return f"{name.upper()} {value}" + ("" if self.is_warm(name) else " (warming)")

    @property
    def arm_name(self) -> str:
        return ARM_NAMES[self.arm] if 0 <= self.arm < len(ARM_NAMES) else f"unknown({self.arm})"

    def describe(self) -> str:
        parts = [
            f"#{self.seq}",
            f"{self.temp_c:.1f} C {self.humidity:.0f} %RH" if self.temp_c is not None
            else ("DHT11 off" if not self.is_on("dht11") else "DHT11 --"),
            " ".join(self._mq(name, value) for name, value in (("mq2", self.mq2), ("mq5", self.mq5), ("mq6", self.mq6))
                     if self.is_fitted(name)),
            f"soil {self.soil_pct:.1f} %" if self.soil_pct is not None
            else ("soil off" if not self.is_on("soil") else "soil --"),
            f"dist {self.distance_cm} cm" if self.distance_cm is not None else "dist --",
            f"arm {self.arm_name}",
            f"batt {self.battery_v:.2f} V",
        ]
        state = [n for n, f in (("MOVING", FLAG_MOVING), ("OBSTACLE", FLAG_OBSTACLE), ("FAILSAFE", FLAG_FAILSAFE))
                 if self.has(f)]
        return " | ".join(parts + state)


def parse_telemetry(body: str) -> Telemetry | None:
    fields = body.split(",")
    if len(fields) not in (12, 15) or fields[0] != "T":  # 12: firmware without sensor switches
        return None
    try:
        nums = [int(x) for x in fields[1:]]
    except ValueError:
        return None
    seq, t, h, mq2, mq5, mq6, soil, dist, arm, mv, flags = nums[:11]
    if len(nums) == 14:
        installed, enabled, warm = nums[11:]
    else:  # older frame: everything fitted is on, warm-up is the single heater flag
        installed = 0b01111 | (0b10000 if mq2 >= 0 else 0)
        enabled = installed
        warm = installed if flags & FLAG_MQ_WARM else installed & 0b00011
    return Telemetry(seq, None if t == -9999 else t / 10, None if h == -9999 else h / 10, mq2, mq5, mq6,
                     None if soil < 0 else soil / 10, None if dist < 0 else dist, arm, mv / 1000, flags,
                     installed, enabled, warm)


# ---------------------------------------------------------------- link

class Transport(Protocol):  # pyserial's Serial fits; tests use an in-process fake Nano
    def write(self, data: bytes) -> int | None: ...
    def readline(self) -> bytes: ...
    def close(self) -> None: ...


class NanoLink:
    """Reads Nano frames in the background and sends commands the way the ESP32 does."""

    def __init__(self, transport: Transport, on_frame: Callable[[str], None] | None = None):
        self.transport = transport
        self.on_frame = on_frame
        self.cond = threading.Condition()
        self.latest: Telemetry | None = None
        self.telemetry_count = 0
        self.frames = self.bad_frames = 0
        self.booted = False
        self.error: str | None = None
        self.replies: deque[str] = deque(maxlen=50)
        self._write_lock = threading.Lock()
        self._running = True
        self._thread = threading.Thread(target=self._read_loop, name="nano-reader", daemon=True)
        self._thread.start()

    def _read_loop(self) -> None:
        while self._running:
            try:
                raw = self.transport.readline()
            except Exception as exc:  # e.g. the USB cable was pulled
                self.error = str(exc)
                time.sleep(0.2)
                continue
            if not raw:
                continue
            text = raw.decode("ascii", "replace").strip()
            body = decode(text)
            with self.cond:
                if body is None:
                    self.bad_frames += text.startswith("$")
                    continue
                self.frames += 1
                if body.startswith("T,"):
                    t = parse_telemetry(body)
                    if t:
                        self.latest, self.telemetry_count = t, self.telemetry_count + 1
                    else:
                        self.bad_frames += 1
                elif body.startswith(("K,", "E,")):
                    self.booted |= body == "K,BOOT"
                    self.replies.append(body)
                self.cond.notify_all()
            if self.on_frame:
                self.on_frame(body)

    def send(self, body: str) -> None:
        with self._write_lock:
            self.transport.write(encode(body))

    def request(self, body: str, timeout: float = 1.0) -> str | None:
        """Send a command and return its "K,<cmd>..." or "E,<cmd>,..." reply (None on timeout)."""
        cmd = body.split(",")[0]
        with self.cond:
            self.replies.clear()
        self.send(body)
        found: list[str] = []

        def reply_arrived() -> bool:
            for r in self.replies:
                if r.split(",")[1:2] == [cmd]:
                    found.append(r)
                    return True
            return False

        return found[0] if self.wait_for(reply_arrived, timeout) else None

    def wait_for(self, pred: Callable[[], bool], timeout: float) -> bool:
        end = time.monotonic() + timeout
        with self.cond:
            while not pred():
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(left)
            return True

    def next_telemetry(self, pred: Callable[[Telemetry], bool] = lambda t: True,
                       timeout: float = 2.0) -> Telemetry | None:
        """First telemetry frame received from now on that satisfies `pred`."""
        start = self.telemetry_count
        found: list[Telemetry] = []

        def ok() -> bool:
            if self.telemetry_count > start and self.latest and pred(self.latest):
                found.append(self.latest)
                return True
            return False

        return found[0] if self.wait_for(ok, timeout) else None

    def drive_for(self, left: int, right: int, seconds: float, period: float = 0.15) -> tuple[bool, list[str]]:
        """Drive with an ESP32-style heartbeat, then stop. Returns (motion seen, rejections such as E,D,arm)."""
        left, right = max(-255, min(255, left)), max(-255, min(255, right))
        with self.cond:
            self.replies.clear()
        moving = False
        end = time.monotonic() + seconds
        try:
            while time.monotonic() < end:
                self.send(f"D,{left},{right}")
                time.sleep(period)
                moving |= bool(self.latest and self.latest.has(FLAG_MOVING))
        finally:
            self.send("S")
        with self.cond:
            rejects = [r for r in self.replies if r.startswith("E,D")]
        return moving, rejects

    def close(self) -> None:
        try:
            self.send("S")
        except Exception:  # port already gone
            pass
        self._running = False
        self._thread.join(timeout=1)
        self.transport.close()


# ---------------------------------------------------------------- automated check

@dataclass
class Result:
    status: str  # PASS | WARN | FAIL | INFO
    name: str
    detail: str
    hint: str = ""


def _check_switches(link: NanoLink, t: Telemetry, add) -> None:
    """Switch the first fitted MQ sensor off and on again (restoring its state) and watch the frames follow."""
    name = next((n for n in ("mq5", "mq6", "mq2") if t.is_fitted(n)), None)
    if name is None:
        return
    was_on = t.is_on(name)
    reply = link.request(f"W,{name},0")
    if reply is None or not reply.startswith("K,W"):
        add("WARN", "sensor switches", reply or "no reply to W,<sensor>,<0|1>",
            "Re-flash nano_controller: this firmware has no per-sensor switches yet.")
        return
    off_ok = link.wait_for(lambda: link.latest is not None and not link.latest.is_on(name), 2.0)
    link.request(f"W,{name},1")
    on_ok = link.wait_for(lambda: link.latest is not None and link.latest.is_on(name), 2.0)
    warming = link.latest is not None and not link.latest.is_warm(name)
    if not was_on:
        link.request(f"W,{name},0")
    if off_ok and on_ok:
        add("PASS", "sensor switches", f"{name.upper()} off -> on; "
            + ("warm-up restarted" if warming else "reading available again"))
    else:
        add("FAIL", "sensor switches", f"{name.upper()} did not follow W,{name},0/1",
            "Check the W command handling and the enable mask in the telemetry frame.")


def run_check(link: NanoLink, *, motors: bool = False, arm: bool = False, report: Callable[[str], None] = print,
              boot_timeout: float = 5.0, window_s: float = 3.0, pulse_s: float = 1.0,
              arm_timeout: float = 10.0) -> list[Result]:
    results: list[Result] = []

    def add(status: str, name: str, detail: str, hint: str = "") -> None:
        results.append(Result(status, name, detail, hint))
        report(f"[{status}] {name}: {detail}")
        if hint and status in ("WARN", "FAIL"):
            report(f"       hint: {hint}")

    if not link.wait_for(lambda: link.booted or link.telemetry_count > 0, boot_timeout):
        add("FAIL", "link", "no frames from the Nano",
            "Wrong COM port or baud (57600)? Disconnect the ESP32 from D0/D1 while using USB. If the upload works but "
            "nothing arrives, re-flash nano_controller.")
        return results
    add("PASS", "link", "boot message received" if link.booted else "telemetry received")
    reply = link.request("P")
    add("PASS" if reply == "K,P" else "FAIL", "ping", reply or "no reply",
        "The Nano sends but does not receive: check the RX line (D0).")

    start, t0 = link.telemetry_count, time.monotonic()
    time.sleep(window_s)
    rate = (link.telemetry_count - start) / (time.monotonic() - t0)
    add("PASS" if rate >= 1.5 else "FAIL" if rate == 0 else "WARN", "telemetry", f"{rate:.1f} frames/s (expected 2)",
        "Slow telemetry usually means a sensor read is blocking (DHT11 or ultrasonic wiring).")
    if link.bad_frames:
        add("WARN", "checksums", f"{link.bad_frames} corrupted frames",
            "Noise on the serial line or a wrong baud rate.")
    t = link.latest
    if t is None:
        return results

    if not t.is_on("dht11"):
        add("INFO", "DHT11", "switched off")
    elif t.has(FLAG_DHT_OK) and t.temp_c is not None and -10 <= t.temp_c <= 60 and 5 <= (t.humidity or 0) <= 100:
        add("PASS", "DHT11", f"{t.temp_c:.1f} C, {t.humidity:.0f} % RH")
    else:
        add("FAIL", "DHT11", "no valid reading", "Check DHT11 data on D2 (10k pull-up to 5 V), VCC and GND.")
    if not t.has(FLAG_MQ_WARM):
        add("INFO", "MQ sensors", "heaters warming up (60 s after power-on or switch-on); values settle after that")
    for name, value in (("MQ2", t.mq2), ("MQ5", t.mq5), ("MQ6", t.mq6)):
        if not t.is_fitted(name.lower()):
            add("INFO", name, "not fitted (HAS_MQ2 is 0 in nano_controller/config.h)")
        elif not t.is_on(name.lower()):
            add("INFO", name, "switched off")
        elif value < 0:
            add("INFO", name, "no reading yet (heater warming up)")
        elif value <= 5 or value >= 1018:
            add("WARN", name, f"raw {value} (stuck at a rail)",
                f"Check the {name} AO wire (A0/A1/A2) and its 5 V supply.")
        else:
            add("PASS", name, f"raw {value}")
    if t.distance_cm is None:
        add("WARN", "ultrasonic", "no echo", "Check HC-SR04 TRIG on D9, ECHO on D10 and 5 V (no object within ~4 m "
            "also gives no echo).")
    else:
        add("PASS", "ultrasonic", f"{t.distance_cm} cm")
    _check_switches(link, t, add)
    if t.battery_v < 1:
        add("INFO", "battery", "0 V: running from USB, or the A6 divider is not wired")
    else:
        add("INFO", "battery", f"{t.battery_v:.2f} V (compare with a multimeter; tune BATTERY_R1_KOHM / R2_KOHM)")
    for body, expect in ((f"C,OBS,{DEFAULT_OBSTACLE_CM}", "K,C,OBS"), (f"C,MAX,{DEFAULT_MAX_PWM}", "K,C,MAX")):
        reply = link.request(body)
        add("PASS" if reply == expect else "FAIL", f"config {body.split(',')[1]}", reply or "no reply")

    def watch_arm(target: int, timeout: float) -> list[str]:
        seen: list[str] = []

        def reached() -> bool:
            if link.latest and (not seen or seen[-1] != link.latest.arm_name):
                seen.append(link.latest.arm_name)
            return bool(link.latest and link.latest.arm == target)

        link.wait_for(reached, timeout)
        return seen

    if arm:
        link.send("S")
        reply = link.request("A,1")
        if reply != "K,A,1":
            add("FAIL", "arm deploy", reply or "no reply", "The Nano refuses to lower the probe while the rover moves.")
        else:
            states = watch_arm(ARM_DEPLOYED, arm_timeout)
            if states[-1:] != [ARM_NAMES[ARM_DEPLOYED]]:
                add("FAIL", "arm deploy", " -> ".join(states) or "no change",
                    "Check the servo on D3, powered from the 5 V buck (not from the Nano's 5 V pin).")
            else:
                soil = link.next_telemetry(lambda x: x.has(FLAG_SOIL_VALID), timeout=3.0)
                if soil is None or soil.soil_pct is None:
                    add("WARN", "arm deploy", f"{' -> '.join(states)}, but no soil reading", "Check the probe on A3.")
                elif soil.soil_pct in (0.0, 100.0):
                    add("WARN", "soil probe", f"{soil.soil_pct:.0f} % (at the calibration limit)",
                        "Calibrate SOIL_DRY_RAW (probe in air) and SOIL_WET_RAW (probe in water) in config.h.")
                else:
                    add("PASS", "arm deploy", f"{' -> '.join(states)}; soil moisture {soil.soil_pct:.1f} %")
                # Safety interlock: the Nano must refuse to drive with the probe in the ground.
                reply = link.request("D,100,100")
                if reply == "E,D,arm":
                    add("PASS", "probe interlock", "drive refused while the probe is down (the Nano raises it)")
                else:
                    link.send("S")
                    add("FAIL", "probe interlock", f"expected E,D,arm, got {reply}",
                        "Do not drive the rover until the interlock works.")
        link.request("A,0")
        states = watch_arm(ARM_RETRACTED, arm_timeout)
        add("PASS" if states[-1:] == [ARM_NAMES[ARM_RETRACTED]] else "FAIL", "arm retract",
            " -> ".join(states) or "no change")

    if motors:
        if link.latest and link.latest.arm != ARM_RETRACTED:
            link.request("A,0")
            watch_arm(ARM_RETRACTED, arm_timeout)
        for name, left, right, watch in (
            ("drive forward", 150, 150, "all six wheels forward"),
            ("drive reverse", -150, -150, "all six wheels backward"),
            ("spin left", -150, 150, "left side backward, right side forward"),
            ("spin right", 150, -150, "left side forward, right side backward"),
        ):
            moving, rejects = link.drive_for(left, right, pulse_s)
            stopped = link.next_telemetry(lambda x: not x.has(FLAG_MOVING), timeout=2.0)
            if rejects:
                add("WARN", name, f"refused: {rejects[0]}", "The probe was down; it is being raised, run again.")
            elif not moving:
                obstacle = link.latest is not None and link.latest.has(FLAG_OBSTACLE) and left > 0 and right > 0
                add("WARN" if obstacle else "FAIL", name, "obstacle stop active" if obstacle else "no motion reported",
                    "Something is within the obstacle distance of the ultrasonic sensor." if obstacle else
                    "Check L298N IN/EN wiring (EN jumpers removed), the motor supply and MIN_PWM.")
            elif stopped is None:
                add("FAIL", name, "did not stop after S", "Unplug the motor supply and check the firmware.")
            else:
                add("PASS", name, f"moved and stopped; check by eye: {watch} (INVERT_LEFT/RIGHT if not)")
        # Failsafe: one drive command, then silence -> the Nano must stop by itself (FAILSAFE_MS = 700 ms).
        t0 = time.monotonic()
        link.send("D,-120,-120")
        started = link.next_telemetry(lambda x: x.has(FLAG_MOVING), timeout=1.5)
        stopped = link.next_telemetry(lambda x: x.has(FLAG_FAILSAFE) and not x.has(FLAG_MOVING), timeout=3.0)
        link.send("S")
        if started is None:
            add("WARN", "failsafe", "the test pulse did not start the motors")
        elif stopped is None:
            add("FAIL", "failsafe", "motors kept running without a heartbeat",
                "Do not run the rover: check FAILSAFE_MS and the firmware version.")
        else:
            add("PASS", "failsafe", f"motors stopped {time.monotonic() - t0:.1f} s after the last command")
    return results


# ---------------------------------------------------------------- interactive console

HELP = """Commands (Enter after each):
  f | b [speed] [secs]     forward / reverse (default 150 for 1 s; heartbeat every 150 ms, then stop)
  l | r [speed] [secs]     spin left / right
  d <left> <right> [secs]  raw wheel targets, -255..255 per side
  s                        stop
  down | up                lower / raise the soil probe
  obs <cm> | max <pwm>     obstacle stop distance (0 = off) / maximum motor PWM
  p                        ping
  t                        show the latest telemetry now
  on|off <dht11|soil|mq5|mq6|mq2|all>   switch a sensor on or off (off = no readings, heater unpowered)
  mute | unmute            pause / resume the telemetry printout (1 per second)
  raw                      toggle printing every raw frame
  q                        stop the motors and quit"""


class Printer:
    def __init__(self, every: float = 1.0):
        self.every, self.muted, self.raw = every, False, False
        self._last = 0.0

    def __call__(self, body: str) -> None:
        if self.raw or not body.startswith("T,"):
            print(f"  < {body}")
        elif not self.muted and time.monotonic() - self._last >= self.every:
            self._last = time.monotonic()
            t = parse_telemetry(body)
            if t:
                print("  " + t.describe())


def interactive(link: NanoLink, printer: Printer) -> None:
    print(HELP)
    moves = {"f": (1, 1), "b": (-1, -1), "l": (-1, 1), "r": (1, -1)}
    while True:
        try:
            line = input("> ").strip()
        except EOFError:
            break
        if not line:
            continue
        cmd, *a = line.split()
        try:
            if cmd == "q":
                break
            if cmd in moves:
                speed, secs = int(a[0]) if a else 150, min(float(a[1]) if len(a) > 1 else 1.0, 10.0)
                link.drive_for(moves[cmd][0] * speed, moves[cmd][1] * speed, secs)
            elif cmd == "d":
                link.drive_for(int(a[0]), int(a[1]), min(float(a[2]) if len(a) > 2 else 1.0, 10.0))
            elif cmd in ("s", "down", "up", "p", "obs", "max"):
                link.send({"s": "S", "down": "A,1", "up": "A,0", "p": "P"}.get(cmd) or
                          f"C,{cmd.upper()},{int(a[0])}")
            elif cmd in ("on", "off"):
                link.send(f"W,{a[0].lower()},{1 if cmd == 'on' else 0}")
            elif cmd == "t":
                print("  " + (link.latest.describe() if link.latest else "no telemetry yet"))
            elif cmd in ("mute", "unmute"):
                printer.muted = cmd == "mute"
            elif cmd == "raw":
                printer.raw = not printer.raw
            else:
                print(HELP)
        except (IndexError, ValueError):
            print("  bad arguments; type 'help'")
        if link.error:
            print(f"  serial error: {link.error}")
            link.error = None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port", nargs="?", help="serial port, e.g. COM5 or /dev/ttyUSB0")
    parser.add_argument("--list", action="store_true", help="list serial ports")
    parser.add_argument("--check", action="store_true", help="run the automated checks")
    parser.add_argument("--arm", action="store_true", help="with --check: lower and raise the soil probe")
    parser.add_argument("--motors", action="store_true", help="with --check: drive pulses + failsafe (wheels up!)")
    parser.add_argument("--baud", type=int, default=BAUD)
    args = parser.parse_args()
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        sys.exit("pyserial is missing: pip install pyserial (or pip install -r requirements-local.txt)")
    if args.list or not args.port:
        ports = list(list_ports.comports())
        for p in ports:
            print(f"{p.device:14s} {p.description}")
        if not ports:
            print("No serial ports found. Is the Nano plugged in? Many clones need the CH340 USB driver.")
        return

    printer = Printer()
    link = NanoLink(serial.Serial(args.port, args.baud, timeout=0.2), on_frame=None if args.check else printer)
    print(f"Opened {args.port} at {args.baud} baud (the Nano restarts when the port opens).")
    code = 0
    try:
        if args.check:
            if args.motors:
                print("--motors turns the wheels: make sure they are OFF the ground. Starting in 3 s (Ctrl+C aborts).")
                time.sleep(3)
            results = run_check(link, motors=args.motors, arm=args.arm)
            counts = {s: sum(r.status == s for r in results) for s in ("PASS", "WARN", "FAIL")}
            print(f"\n{counts['PASS']} passed, {counts['WARN']} warnings, {counts['FAIL']} failed")
            code = 1 if counts["FAIL"] else 0
        else:
            interactive(link, printer)
    except KeyboardInterrupt:
        pass
    finally:
        link.close()
    sys.exit(code)


if __name__ == "__main__":
    main()
