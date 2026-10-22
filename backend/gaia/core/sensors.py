"""Sensor registry: units, valid ranges, interpretation bands and query keywords.

Adding a sensor to the rover only requires sending a new key in telemetry; add
an entry here to give it a proper label, unit and interpretation bands.
Unregistered keys are still stored and summarised with a generic spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Band:
    lo: float | None
    hi: float | None
    label: str
    note: str = ""

    def contains(self, value: float) -> bool:
        return (self.lo is None or value >= self.lo) and (self.hi is None or value < self.hi)


@dataclass(frozen=True)
class SensorSpec:
    key: str
    label: str
    unit: str
    hardware: str
    description: str
    valid_min: float
    valid_max: float
    decimals: int = 1
    rag: bool = True  # included in RAG documents / chat facts
    percent_scale: bool = False  # differences reported in percentage points
    keywords: tuple[str, ...] = ()
    bands: tuple[Band, ...] = field(default_factory=tuple)

    def fmt(self, value: float) -> str:
        return f"{value:.{self.decimals}f}"

    def with_unit(self, value: float) -> str:
        sep = "" if self.unit in ("%", "°C") else " "
        return f"{self.fmt(value)}{sep}{self.unit}".strip()

    def band_for(self, value: float) -> Band | None:
        return next((b for b in self.bands if b.contains(value)), None)

    def delta_text(self, delta: float) -> str:
        unit = "percentage points" if self.percent_scale else self.unit
        return f"{abs(delta):.{self.decimals}f} {unit}".strip()


_MQ_NOTE = (
    "Raw 10-bit ADC value from an uncalibrated MQ sensor; treat it as a relative gas "
    "index, not a ppm concentration."
)

SENSORS: dict[str, SensorSpec] = {
    "temperature": SensorSpec(
        key="temperature",
        label="Air temperature",
        unit="°C",
        hardware="DHT11",
        description="Air temperature around the rover (DHT11, ±2 °C).",
        valid_min=-10,
        valid_max=60,
        keywords=("temperature", "temp", "hot", "heat", "cold", "warm", "cool", "celsius", "degree"),
        bands=(
            Band(None, 10, "cold", "cold stress is possible for most warm-season crops"),
            Band(10, 18, "cool", "slower growth for warm-season crops"),
            Band(18, 32, "comfortable", "within the comfortable range for most crops"),
            Band(32, 38, "hot", "heat stress possible, especially with low soil moisture"),
            Band(38, None, "very hot", "high heat stress risk; flower and fruit drop likely"),
        ),
    ),
    "humidity": SensorSpec(
        key="humidity",
        label="Relative humidity",
        unit="%",
        hardware="DHT11",
        description="Relative air humidity (DHT11, ±5 %).",
        valid_min=0,
        valid_max=100,
        decimals=0,
        percent_scale=True,
        keywords=("humid", "humidity", "damp", "muggy", "air moisture", "rh"),
        bands=(
            Band(None, 30, "very dry air", "high evaporation and transpiration demand"),
            Band(30, 60, "moderate", "comfortable for most crops"),
            Band(60, 85, "humid", "watch for prolonged leaf wetness"),
            Band(85, None, "very humid", "elevated fungal disease pressure (blights, mildews)"),
        ),
    ),
    "soil_moisture": SensorSpec(
        key="soil_moisture",
        label="Soil moisture",
        unit="%",
        hardware="Capacitive soil probe on servo arm",
        description=(
            "Volumetric soil moisture estimate from the capacitive probe. Only measured "
            "while the rover is parked with the probe arm lowered."
        ),
        valid_min=0,
        valid_max=100,
        percent_scale=True,
        keywords=("soil", "moisture", "water", "watering", "irrigat", "dry", "wet", "drought"),
        bands=(
            Band(None, 25, "dry", "irrigation is likely needed soon"),
            Band(25, 60, "adequate", "suitable for most crops"),
            Band(60, 80, "moist", "irrigation can usually wait"),
            Band(80, None, "saturated", "waterlogging and root-disease risk"),
        ),
    ),
    "mq2": SensorSpec(
        key="mq2",
        label="MQ2 gas index (smoke / combustible gases)",
        unit="ADC",
        hardware="MQ2",
        description="Smoke, LPG and combustible-gas sensor. " + _MQ_NOTE,
        valid_min=0,
        valid_max=1023,
        decimals=0,
        keywords=("mq2", "mq-2", "smoke", "combustible", "fire", "burning"),
        bands=(
            Band(None, 300, "baseline", "no unusual smoke or combustible gas signal"),
            Band(300, 500, "elevated", "elevated signal; check for smoke or burning nearby"),
            Band(500, None, "high", "high signal; possible smoke or gas leak"),
        ),
    ),
    "mq5": SensorSpec(
        key="mq5",
        label="MQ5 gas index (LPG / natural gas / methane)",
        unit="ADC",
        hardware="MQ5",
        description="LPG, natural gas and methane (e.g. biogas, manure) sensor. " + _MQ_NOTE,
        valid_min=0,
        valid_max=1023,
        decimals=0,
        keywords=("mq5", "mq-5", "methane", "natural gas", "biogas", "manure"),
        bands=(
            Band(None, 300, "baseline", "no unusual methane or natural-gas signal"),
            Band(300, 500, "elevated", "elevated signal; possible methane from manure or biogas"),
            Band(500, None, "high", "high signal; investigate possible gas source"),
        ),
    ),
    "mq6": SensorSpec(
        key="mq6",
        label="MQ6 gas index (LPG / butane / propane)",
        unit="ADC",
        hardware="MQ6",
        description="LPG, butane and propane sensor. " + _MQ_NOTE,
        valid_min=0,
        valid_max=1023,
        decimals=0,
        keywords=("mq6", "mq-6", "lpg", "butane", "propane", "cylinder"),
        bands=(
            Band(None, 300, "baseline", "no unusual LPG signal"),
            Band(300, 500, "elevated", "elevated LPG/butane signal"),
            Band(500, None, "high", "high LPG signal; check for leaks"),
        ),
    ),
    # Rover channels: stored and shown in the app, but not part of the RAG corpus.
    "distance_cm": SensorSpec(
        key="distance_cm",
        label="Obstacle distance",
        unit="cm",
        hardware="HC-SR04 ultrasonic",
        description="Distance to the nearest obstacle in front of the rover.",
        valid_min=0,
        valid_max=500,
        decimals=0,
        rag=False,
    ),
    "battery_v": SensorSpec(
        key="battery_v",
        label="Battery voltage",
        unit="V",
        hardware="Voltage divider",
        description="Rover battery voltage.",
        valid_min=0,
        valid_max=30,
        decimals=2,
        rag=False,
    ),
}

GAS_SENSORS = ("mq2", "mq5", "mq6")


@dataclass(frozen=True)
class ToggleSpec:
    """A sensor the farmer can switch on and off. One switch may power several reading keys."""

    key: str
    label: str
    short: str
    sensors: tuple[str, ...]
    reason_no_reading: str  # why a switched-on sensor can still have no reading


TOGGLES: dict[str, ToggleSpec] = {
    "dht11": ToggleSpec("dht11", "DHT11 temperature and humidity sensor", "DHT11", ("temperature", "humidity"),
                        "no valid reading from the sensor"),
    "soil_moisture": ToggleSpec("soil_moisture", "soil moisture probe", "soil probe", ("soil_moisture",),
                                "the probe was not lowered or the rover was moving"),
    "mq5": ToggleSpec("mq5", "MQ5 gas sensor", "MQ5", ("mq5",), "the heater was still warming up"),
    "mq6": ToggleSpec("mq6", "MQ6 gas sensor", "MQ6", ("mq6",), "the heater was still warming up"),
    "mq2": ToggleSpec("mq2", "MQ2 gas sensor", "MQ2", ("mq2",), "the heater was still warming up"),
}
TOGGLE_ORDER = tuple(TOGGLES)


def toggle_for_sensor(sensor: str) -> str | None:
    """The switch that powers a reading key (None for rover channels such as distance or battery)."""
    return next((t.key for t in TOGGLES.values() if sensor in t.sensors), None)

# Accept a few common aliases from firmware / relays.
ALIASES = {
    "temp": "temperature",
    "temperature_c": "temperature",
    "hum": "humidity",
    "humidity_pct": "humidity",
    "rh": "humidity",
    "soil": "soil_moisture",
    "soil_pct": "soil_moisture",
    "soilmoisture": "soil_moisture",
    "distance": "distance_cm",
    "battery": "battery_v",
}

_KEY_RE = re.compile(r"^[a-z0-9_]{1,32}$")


def normalize_key(key: str) -> str | None:
    k = key.strip().lower().replace("-", "_").replace(" ", "_")
    k = ALIASES.get(k, k)
    return k if _KEY_RE.match(k) else None


def get_spec(key: str) -> SensorSpec:
    spec = SENSORS.get(key)
    if spec:
        return spec
    return SensorSpec(
        key=key,
        label=key.replace("_", " ").capitalize(),
        unit="",
        hardware="unregistered sensor",
        description=f"Unregistered sensor channel '{key}'.",
        valid_min=-1e9,
        valid_max=1e9,
        decimals=2,
    )


def rag_sensor_keys() -> list[str]:
    return [k for k, s in SENSORS.items() if s.rag]


def public_registry() -> list[dict]:
    return [
        {
            "key": s.key,
            "label": s.label,
            "unit": s.unit,
            "hardware": s.hardware,
            "description": s.description,
            "valid_min": s.valid_min,
            "valid_max": s.valid_max,
            "decimals": s.decimals,
            "rag": s.rag,
            "bands": [{"lo": b.lo, "hi": b.hi, "label": b.label, "note": b.note} for b in s.bands],
        }
        for s in SENSORS.values()
    ]
