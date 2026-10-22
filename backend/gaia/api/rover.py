"""Rover relay API: the web UI drives the rover through the backend."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from ..container import Container
from ..ingestion.telemetry import TelemetryBatch, ingest_batch
from ..rover.client import RoverUnavailable
from .deps import get_container, get_db, require_api_key

router = APIRouter(prefix="/rover", tags=["rover"], dependencies=[Depends(require_api_key)])


class DriveCommand(BaseModel):
    """Either tank-style wheel speeds, a joystick vector, or a named direction."""

    left: int | None = Field(default=None, ge=-255, le=255)
    right: int | None = Field(default=None, ge=-255, le=255)
    throttle: float | None = Field(default=None, ge=-1, le=1)
    steer: float | None = Field(default=None, ge=-1, le=1)
    dir: Literal["forward", "reverse", "left", "right", "stop"] | None = None
    speed: int = Field(default=180, ge=0, le=255)

    @model_validator(mode="after")
    def _one_form(self):
        forms = [self.left is not None or self.right is not None, self.throttle is not None or self.steer is not None,
                 self.dir is not None]
        if sum(forms) != 1:
            raise ValueError("Send exactly one of: left/right, throttle/steer, or dir")
        return self


class ArmCommand(BaseModel):
    action: Literal["deploy", "retract"]


class SensorSwitches(BaseModel):
    """Switch sensors on or off: ``{"switches": {"mq5": false}}`` or ``{"all": false}`` (video only)."""

    switches: dict[Literal["dht11", "soil_moisture", "mq5", "mq6", "mq2"], bool] | None = None
    all: bool | None = None

    @model_validator(mode="after")
    def _one_form(self):
        if (self.switches is None) == (self.all is None):
            raise ValueError("Send exactly one of: switches, all")
        return self


def _call(fn, *args):
    try:
        return fn(*args)
    except RoverUnavailable as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/config")
def rover_config(c: Container = Depends(get_container)):
    return {"base_url": c.rover.base_url, "stream_url": c.rover.stream_url}


@router.get("/status")
def rover_status(c: Container = Depends(get_container)):
    return _call(c.rover.status)


@router.get("/telemetry")
def rover_telemetry(c: Container = Depends(get_container)):
    return _call(c.rover.telemetry)


@router.post("/drive")
def rover_drive(cmd: DriveCommand, c: Container = Depends(get_container)):
    return _call(c.rover.drive, cmd.model_dump(exclude_none=True))


@router.post("/stop")
def rover_stop(c: Container = Depends(get_container)):
    return _call(c.rover.stop)


@router.post("/arm")
def rover_arm(cmd: ArmCommand, c: Container = Depends(get_container)):
    return _call(c.rover.arm, cmd.action)


@router.get("/sensors")
def rover_sensors(c: Container = Depends(get_container)):
    return _call(c.rover.sensors)


@router.post("/sensors")
def rover_set_sensors(cmd: SensorSwitches, c: Container = Depends(get_container)):
    body = {"all": cmd.all} if cmd.all is not None else dict(cmd.switches)
    return _call(c.rover.set_sensors, body)


@router.get("/capture")
def rover_capture(c: Container = Depends(get_container)):
    return Response(_call(c.rover.capture), media_type="image/jpeg")


@router.post("/telemetry/pull")
def pull_telemetry(s: Session = Depends(get_db), c: Container = Depends(get_container)):
    """Fetch the rover's current reading and store it (server-side relay)."""
    t = _call(c.rover.telemetry)
    batch = TelemetryBatch.model_validate(
        {"device_id": t.get("device_id", "rover-01"), "readings": [{"values": t.get("values", {}),
                                                                    "state": t.get("state")}],
         "sensor_switches": (t.get("state") or {}).get("sensor_switches")}
    )
    result = ingest_batch(s, batch, c.settings)
    s.commit()
    return result.to_dict()
