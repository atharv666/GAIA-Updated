"""Synthetic but plausible field conditions, shared by the seeder and the rover simulator."""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta


class FarmModel:
    """Diurnal temperature/humidity, drying soil with irrigation events, MQ baselines with rare spikes."""

    def __init__(self, seed: int = 7, spike_probability: float = 0.002):
        self.rng = random.Random(seed)
        self.soil = 55.0
        self.last_ts: datetime | None = None
        self.spike_until: datetime | None = None
        self.spike_probability = spike_probability

    def force_gas_spike(self, local: datetime, minutes: int = 20) -> None:
        self.spike_until = local + timedelta(minutes=minutes)

    def sample(self, local: datetime) -> dict[str, float]:
        h = local.hour + local.minute / 60
        day_wave = math.sin((h - 9) / 24 * 2 * math.pi)  # warmest mid-afternoon
        temp = 27 + 6 * day_wave + self.rng.gauss(0, 0.4)
        humidity = min(98, max(25, 66 - 18 * day_wave + self.rng.gauss(0, 1.5)))

        if self.last_ts is not None:
            dt_h = max(0.0, (local - self.last_ts).total_seconds() / 3600)
            # Evapotranspiration: faster when hot and dry.
            self.soil -= dt_h * (0.35 + 0.05 * max(0, temp - 25) + 0.01 * max(0, 60 - humidity))
            if self.soil < 24 and self.rng.random() < 0.5:
                self.soil += self.rng.uniform(28, 36)  # irrigation event
        self.soil = min(85, max(12, self.soil))
        self.last_ts = local

        if self.spike_until is None or local >= self.spike_until:
            self.spike_until = None
            if self.rng.random() < self.spike_probability:
                self.force_gas_spike(local)
        gas_boost = 260.0 if self.spike_until else 0.0
        return {
            "temperature": round(temp, 1),
            "humidity": round(humidity, 0),
            "soil_moisture": round(self.soil + self.rng.gauss(0, 0.6), 1),
            "mq2": round(170 + self.rng.gauss(0, 12) + gas_boost * 0.4),
            "mq5": round(205 + self.rng.gauss(0, 15) + gas_boost),
            "mq6": round(190 + self.rng.gauss(0, 12) + gas_boost * 0.6),
        }
