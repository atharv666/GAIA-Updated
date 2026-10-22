from datetime import timedelta

from gaia.db.base import utcnow


def test_health_and_config(client):
    h = client.get("/api/v1/health").json()
    assert h["status"] == "ok" and h["database"] == "sqlite"
    cfg = client.get("/api/v1/config").json()
    assert {s["key"] for s in cfg["sensors"]} >= {"temperature", "humidity", "soil_moisture", "mq2", "mq5", "mq6"}


def test_telemetry_requires_device_key_and_feeds_latest(client):
    payload = {"device_id": "rover-01", "readings": [{"values": {"temperature": 27.5, "mq5": 230}}]}
    assert client.post("/api/v1/telemetry", json=payload).status_code == 401
    r = client.post("/api/v1/telemetry", json=payload, headers={"X-Device-Key": "test-device-key"})
    assert r.status_code == 200 and r.json()["accepted"] == 2

    latest = {x["sensor"]: x for x in client.get("/api/v1/telemetry/latest").json()["readings"]}
    assert latest["temperature"]["display"] == "27.5°C" and latest["temperature"]["band"] == "comfortable"
    assert latest["mq5"]["band"] == "baseline"


def test_history_and_summary_after_sync(client):
    now = utcnow()
    readings = [
        {"ts": (now - timedelta(minutes=40) + timedelta(minutes=i)).isoformat(), "values": {"humidity": 60 + i % 3}}
        for i in range(30)
    ]
    client.post("/api/v1/telemetry", json={"device_id": "rover-01", "readings": readings},
                headers={"X-Device-Key": "test-device-key"})
    sync = client.post("/api/v1/ingest/sync", json={"include_open": True}).json()
    assert sync["windows_aggregated"] >= 2
    hist = client.get("/api/v1/telemetry/history", params={"sensor": "humidity", "hours": 2}).json()
    assert sum(p["count"] for p in hist["points"]) == 30
    summary = client.get("/api/v1/telemetry/summary", params={"range": "last_hours:2"}).json()
    assert summary["stats"][0]["sensor"] == "humidity" and summary["stats"][0]["count"] == 30
    assert client.get("/api/v1/telemetry/summary", params={"range": "bogus"}).status_code == 422
    ov = client.get("/api/v1/overview").json()
    assert ov["readings"] == 30


def test_notes_documents_and_search(client):
    r = client.post("/api/v1/knowledge/notes", json={
        "title": "Planting log", "text": "Planted tomato variety Arka Rakshak in the north plot on 2 September.",
    })
    assert r.status_code == 200 and len(r.json()["chunk_ids"]) == 1
    doc = "Drip irrigation schedule.\n\n" + ("Irrigate the chilli beds for 40 minutes every morning. " * 60)
    r = client.post("/api/v1/knowledge/documents", files={"file": ("irrigation.md", doc.encode(), "text/markdown")})
    assert r.status_code == 200
    doc_chunks = len(r.json()["chunk_ids"])
    assert doc_chunks >= 2
    assert client.post("/api/v1/knowledge/documents",
                       files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 415

    hits = client.get("/api/v1/knowledge/search", params={"q": "which tomato variety did I plant"}).json()["results"]
    assert hits[0]["title"] == "Planting log"
    assert hits[0]["lexical_rank"] == 1

    stats = client.get("/api/v1/knowledge/stats").json()
    assert stats["by_type"] == {"farm_note": 1, "document": doc_chunks}
    docs = client.get("/api/v1/knowledge/documents").json()
    note_id = next(d["id"] for d in docs if d["kind"] == "note")
    assert client.delete(f"/api/v1/knowledge/documents/{note_id}").json()["knowledge_chunks_removed"] == 1


def test_api_key_enforced_when_configured(client, container):
    container.settings.api_key = "secret"
    try:
        assert client.get("/api/v1/overview").status_code == 401
        assert client.get("/api/v1/overview", headers={"X-API-Key": "secret"}).status_code == 200
        assert client.get("/api/v1/health").status_code == 200  # health stays public
    finally:
        container.settings.api_key = None


def test_rover_relay_reports_unreachable_rover(client, container):
    container.rover.base_url = "http://127.0.0.1:9"
    container.rover._http.base_url = "http://127.0.0.1:9"
    r = client.post("/api/v1/rover/drive", json={"dir": "forward", "speed": 150})
    assert r.status_code == 502
    assert client.post("/api/v1/rover/drive", json={"left": 10, "dir": "forward"}).status_code == 422


def test_web_pages_render(client):
    for path in ("/", "/chat", "/diagnose", "/sensors", "/knowledge", "/rover"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "GAIA" in r.text
    assert client.get("/static/css/gaia.css").status_code == 200


def test_web_search_ranks_quality_and_drops_user_generated():
    from gaia.providers.websearch import WebResult, WebSearch

    class Canned(WebSearch):
        name = "canned"

        def __init__(self, rows):
            self.rows = rows

        def search(self, query, max_results):
            return self.rows

    rows = [
        WebResult("Thread", "https://www.reddit.com/r/gardening/x", "…"),
        WebResult("Blog", "https://example-farm-blog.com/blight", "…"),
        WebResult("TNAU", "https://agritech.tnau.ac.in/crop_protection/tomato.html", "…"),
    ]
    ranked = Canned(rows).search_many(["q"], 5)
    assert [r.quality for r in ranked] == ["institutional", "general"]
    only_forums = Canned([WebResult("Q", "https://www.quora.com/x", "…")]).search_many(["q"], 5)
    assert only_forums[0].quality == "user_generated"  # kept when nothing better exists
