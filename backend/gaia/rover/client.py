"""HTTP client for the ESP32-CAM rover API (see firmware/PROTOCOL.md).

The backend relays commands so the web UI can drive the rover when the
backend machine is on the same network as the rover.
"""

from __future__ import annotations

import httpx


class RoverUnavailable(RuntimeError):
    pass


class RoverClient:
    def __init__(self, base_url: str, stream_url: str, timeout: float = 5.0):
        self.base_url = base_url.rstrip("/")
        self.stream_url = stream_url
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout)

    def _request(self, method: str, path: str, **kw) -> httpx.Response:
        try:
            r = self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise RoverUnavailable(f"Rover not reachable at {self.base_url}: {exc}") from exc
        if r.status_code >= 400:
            raise RoverUnavailable(f"Rover returned HTTP {r.status_code}: {r.text[:200]}")
        return r

    def status(self) -> dict:
        return self._request("GET", "/api/status").json()

    def telemetry(self) -> dict:
        return self._request("GET", "/api/telemetry").json()

    def drive(self, payload: dict) -> dict:
        return self._request("POST", "/api/drive", json=payload).json()

    def stop(self) -> dict:
        return self._request("POST", "/api/stop").json()

    def arm(self, action: str) -> dict:
        return self._request("POST", "/api/arm", json={"action": action}).json()

    def sensors(self) -> dict:
        return self._request("GET", "/api/sensors").json()

    def set_sensors(self, switches: dict[str, bool]) -> dict:
        return self._request("POST", "/api/sensors", json=switches).json()

    def capture(self) -> bytes:
        r = self._request("GET", "/capture", timeout=15.0)
        if not r.headers.get("content-type", "").startswith("image/"):
            raise RoverUnavailable("Rover /capture did not return an image")
        return r.content
