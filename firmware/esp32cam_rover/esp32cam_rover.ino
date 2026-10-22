// GAIA rover - ESP32-CAM firmware (board: "AI Thinker ESP32-CAM", PSRAM enabled).
//
//  * Wi-Fi: joins the farm / phone hotspot, falls back to its own hotspot (config.h)
//  * Port 80: control API (/api/*), high-resolution stills (/capture), snapshot
//  * Port 81: MJPEG live stream (/stream) - separate server so streaming never blocks control
//  * UART to the Arduino Nano (motors + sensors), with a drive heartbeat watchdog
//  * Telemetry sampled every 5 s, queued, and pushed in batches to the GAIA backend
//
// Libraries: ArduinoJson 7.x (Benoit Blanchon). Everything else ships with the ESP32 core.
// HTTP API and UART protocol: firmware/PROTOCOL.md

#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESPmDNS.h>
#include <HTTPClient.h>
#include <WiFi.h>
#include <time.h>

#include "esp_camera.h"
#include "esp_http_server.h"

#include "camera_pins.h"
#include "config.h"
#include "nano_link.h"

#define FIRMWARE_VERSION "gaia-esp32cam-1.1"

HardwareSerial NanoSerial(1);
NanoLink nano;

SemaphoreHandle_t camMutex;
bool cameraOk = false;
framesize_t captureFramesize = CAPTURE_FRAMESIZE;
httpd_handle_t controlServer = nullptr;
httpd_handle_t streamServer = nullptr;
String wifiModeName = "none";

// Drive state (written by the HTTP task, watched by loop()).
volatile int driveLeft = 0;
volatile int driveRight = 0;
volatile uint32_t lastDriveMs = 0;

// Telemetry queue (only touched by the telemetry task).
struct QueuedReading {
  uint32_t uptimeMs;
  uint32_t epoch;
  NanoTelemetry t;
};
QueuedReading queue[QUEUE_CAPACITY];
size_t qHead = 0;
volatile size_t qCount = 0;
volatile bool lastPushOk = false;
uint32_t pushIntervalMs = PUSH_PERIOD_MS;
volatile bool pushNow = false;  // a sensor switch changed: tell the backend straight away

// ---------------------------------------------------------------- helpers

bool staConnected() { return WiFi.status() == WL_CONNECTED; }
bool timeSynced() { return time(nullptr) > 1700000000; }

// {"dht11":true,"soil_moisture":false,...} for every sensor that is fitted.
void fillSwitches(JsonObject o, const NanoTelemetry &t) {
  for (int i = 0; i < SENSOR_COUNT; i++) {
    if (t.fitted(i)) o[SENSOR_KEYS[i]] = t.on(i);
  }
}

void fillReading(JsonObject r, const NanoTelemetry &t, uint32_t uptimeMs, uint32_t epoch) {
  r["uptime_ms"] = uptimeMs;
  if (epoch) r["ts"] = epoch;
  JsonObject v = r["values"].to<JsonObject>();
  if (t.has(NANO_DHT_OK) && t.tempX10 != -9999) {
    v["temperature"] = t.tempX10 / 10.0;
    v["humidity"] = t.humX10 / 10.0;
  }
  // A value of -1 means: not fitted, switched off, or still warming up. Old Nano firmware always sends
  // values, so there the single heater-warm flag decides.
  if (t.hasMasks || t.has(NANO_MQ_WARM)) {
    if (t.mq2 >= 0) v["mq2"] = t.mq2;
    if (t.mq5 >= 0) v["mq5"] = t.mq5;
    if (t.mq6 >= 0) v["mq6"] = t.mq6;
  }
  bool soil = t.has(NANO_SOIL_VALID) && t.soilX10 >= 0;
  if (soil) v["soil_moisture"] = t.soilX10 / 10.0;
  if (t.distanceCm >= 0) v["distance_cm"] = t.distanceCm;
  if (t.batteryMv > 0) v["battery_v"] = t.batteryMv / 1000.0;
  JsonObject s = r["state"].to<JsonObject>();
  s["moving"] = t.has(NANO_MOVING);
  s["arm_deployed"] = t.arm == 3;
  s["soil_valid"] = soil;
  s["obstacle"] = t.has(NANO_OBSTACLE);
  s["failsafe"] = t.has(NANO_FAILSAFE);
  if (t.hasMasks) {
    fillSwitches(s["sensor_switches"].to<JsonObject>(), t);
    JsonArray warming = s["warming"].to<JsonArray>();
    for (int i = 0; i < SENSOR_COUNT; i++) {
      if (t.fitted(i) && t.on(i) && !t.warmed(i)) warming.add(SENSOR_KEYS[i]);
    }
  }
}

static void addCors(httpd_req_t *req) {
  httpd_resp_set_hdr(req, "Access-Control-Allow-Origin", "*");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Headers", "Content-Type, X-API-Key");
  httpd_resp_set_hdr(req, "Access-Control-Allow-Methods", "GET, POST, OPTIONS");
}

static esp_err_t sendJson(httpd_req_t *req, JsonDocument &doc, const char *status = "200 OK") {
  String out;
  serializeJson(doc, out);
  httpd_resp_set_status(req, status);
  httpd_resp_set_type(req, "application/json");
  addCors(req);
  return httpd_resp_send(req, out.c_str(), out.length());
}

static esp_err_t sendError(httpd_req_t *req, const char *status, const char *message) {
  JsonDocument doc;
  doc["ok"] = false;
  doc["error"] = message;
  return sendJson(req, doc, status);
}

static bool readJsonBody(httpd_req_t *req, JsonDocument &doc) {
  char buf[256];
  int total = req->content_len;
  if (total <= 0 || total >= (int)sizeof(buf)) return false;
  int received = 0;
  while (received < total) {
    int r = httpd_req_recv(req, buf + received, total - received);
    if (r == HTTPD_SOCK_ERR_TIMEOUT) continue;
    if (r <= 0) return false;
    received += r;
  }
  buf[received] = '\0';
  return deserializeJson(doc, buf) == DeserializationError::Ok;
}

static bool queryFlag(httpd_req_t *req, const char *key) {
  char query[64];
  char value[8];
  if (httpd_req_get_url_query_str(req, query, sizeof(query)) != ESP_OK) return false;
  if (httpd_query_key_value(query, key, value, sizeof(value)) != ESP_OK) return false;
  return value[0] == '1' || value[0] == 't';
}

static void setDrive(int left, int right) {
  driveLeft = left;
  driveRight = right;
  lastDriveMs = millis();
  if (left == 0 && right == 0) nano.stop();
  else nano.drive(left, right);
}

// ---------------------------------------------------------------- handlers

static esp_err_t rootHandler(httpd_req_t *req) {
  static const char page[] =
      "<!doctype html><meta name=viewport content='width=device-width,initial-scale=1'>"
      "<title>GAIA rover</title><body style='font-family:sans-serif;margin:20px'>"
      "<h2>GAIA rover</h2><img id=s style='width:100%;max-width:640px;border-radius:12px'>"
      "<p><a href='/api/status'>/api/status</a> &middot; <a href='/api/telemetry'>/api/telemetry</a> "
      "&middot; <a href='/capture'>/capture</a></p>"
      "<script>s.src=location.protocol+'//'+location.hostname+':81/stream'</script>";
  httpd_resp_set_type(req, "text/html");
  return httpd_resp_send(req, page, sizeof(page) - 1);
}

static esp_err_t preflightHandler(httpd_req_t *req) {
  httpd_resp_set_status(req, "204 No Content");
  addCors(req);
  return httpd_resp_send(req, nullptr, 0);
}

static esp_err_t statusHandler(httpd_req_t *req) {
  NanoTelemetry t = nano.latest();
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["firmware"] = FIRMWARE_VERSION;
  doc["uptime_ms"] = millis();
  JsonObject wifi = doc["wifi"].to<JsonObject>();
  wifi["mode"] = wifiModeName;
  if (staConnected()) {
    wifi["ssid"] = WiFi.SSID();
    wifi["rssi"] = WiFi.RSSI();
    wifi["ip"] = WiFi.localIP().toString();
  } else {
    wifi["ssid"] = AP_SSID;
    wifi["ip"] = WiFi.softAPIP().toString();
  }
  wifi["ap_clients"] = WiFi.softAPgetStationNum();
  JsonObject link = doc["nano"].to<JsonObject>();
  link["connected"] = nano.connected();
  if (t.valid) link["last_seen_ms"] = millis() - t.receivedMs;
  doc["camera"]["ok"] = cameraOk;
  doc["drive"]["left"] = (int)driveLeft;
  doc["drive"]["right"] = (int)driveRight;
  doc["arm"] = t.valid ? armName(t.arm) : "unknown";
  if (t.valid && t.hasMasks) fillSwitches(doc["sensors"]["switches"].to<JsonObject>(), t);
  if (t.distanceCm >= 0) doc["obstacle_cm"] = t.distanceCm;
  if (t.batteryMv > 0) doc["battery_v"] = t.batteryMv / 1000.0;
  JsonObject backend = doc["backend"].to<JsonObject>();
  backend["url"] = BACKEND_URL;
  backend["last_push_ok"] = (bool)lastPushOk;
  backend["queued"] = (uint32_t)qCount;
  backend["time_synced"] = timeSynced();
  return sendJson(req, doc);
}

static esp_err_t telemetryHandler(httpd_req_t *req) {
  NanoTelemetry t = nano.latest();
  if (!t.valid) return sendError(req, "503 Service Unavailable", "no telemetry from the Nano yet");
  JsonDocument doc;
  JsonObject root = doc.to<JsonObject>();
  root["device_id"] = DEVICE_ID;
  fillReading(root, t, millis(), timeSynced() ? (uint32_t)time(nullptr) : 0);
  return sendJson(req, doc);
}

static esp_err_t driveHandler(httpd_req_t *req) {
  JsonDocument body;
  if (!readJsonBody(req, body)) return sendError(req, "400 Bad Request", "invalid JSON body");
  int left = 0, right = 0;
  if (!body["left"].isNull() || !body["right"].isNull()) {
    left = body["left"].as<int>();
    right = body["right"].as<int>();
  } else if (!body["throttle"].isNull() || !body["steer"].isNull()) {
    float th = body["throttle"].as<float>(), st = body["steer"].as<float>();
    left = (int)(255 * constrain(th + st, -1.0f, 1.0f));
    right = (int)(255 * constrain(th - st, -1.0f, 1.0f));
  } else if (body["dir"].is<const char *>()) {
    const char *dir = body["dir"].as<const char *>();
    int s = body["speed"].isNull() ? 180 : constrain(body["speed"].as<int>(), 0, 255);
    if (!strcmp(dir, "forward")) { left = s; right = s; }
    else if (!strcmp(dir, "reverse")) { left = -s; right = -s; }
    else if (!strcmp(dir, "left")) { left = -s; right = s; }
    else if (!strcmp(dir, "right")) { left = s; right = -s; }
    else if (strcmp(dir, "stop")) return sendError(req, "400 Bad Request", "unknown dir");
  } else {
    return sendError(req, "400 Bad Request", "send left/right, throttle/steer or dir");
  }
  left = constrain(left, -255, 255);
  right = constrain(right, -255, 255);
  setDrive(left, right);

  NanoTelemetry t = nano.latest();
  JsonDocument doc;
  doc["ok"] = true;
  if ((left || right) && t.valid && t.arm != 0) {
    doc["held"] = "soil probe retracting";  // the Nano raises the probe before it moves
  } else if (left > 0 && right > 0 && t.has(NANO_OBSTACLE)) {
    doc["ok"] = false;
    doc["blocked"] = "obstacle";
    doc["distance_cm"] = t.distanceCm;
  } else {
    doc["left"] = left;
    doc["right"] = right;
  }
  return sendJson(req, doc);
}

static esp_err_t stopHandler(httpd_req_t *req) {
  setDrive(0, 0);
  JsonDocument doc;
  doc["ok"] = true;
  return sendJson(req, doc);
}

static esp_err_t armHandler(httpd_req_t *req) {
  JsonDocument body;
  if (!readJsonBody(req, body) || !body["action"].is<const char *>()) {
    return sendError(req, "400 Bad Request", "expected {\"action\":\"deploy\"|\"retract\"}");
  }
  const char *action = body["action"].as<const char *>();
  bool deploy = !strcmp(action, "deploy");
  if (!deploy && strcmp(action, "retract")) return sendError(req, "400 Bad Request", "unknown action");
  if (deploy && (driveLeft || driveRight)) return sendError(req, "409 Conflict", "stop the rover before lowering the probe");
  nano.arm(deploy);
  JsonDocument doc;
  doc["ok"] = true;
  doc["arm"] = deploy ? "deploying" : "retracting";
  return sendJson(req, doc);
}

static void writeSensorState(JsonDocument &doc, const NanoTelemetry &t, int overrideIdx = -1, bool overrideOn = false,
                             bool overrideAll = false) {
  JsonObject sw = doc["switches"].to<JsonObject>();
  JsonArray warming = doc["warming"].to<JsonArray>();
  JsonArray installed = doc["installed"].to<JsonArray>();
  for (int i = 0; i < SENSOR_COUNT; i++) {
    if (!t.fitted(i)) continue;
    bool on = overrideAll ? overrideOn : (i == overrideIdx ? overrideOn : t.on(i));
    sw[SENSOR_KEYS[i]] = on;
    installed.add(SENSOR_KEYS[i]);
    if (on && !t.warmed(i)) warming.add(SENSOR_KEYS[i]);
  }
}

static esp_err_t sensorsGetHandler(httpd_req_t *req) {
  NanoTelemetry t = nano.latest();
  if (!t.valid) return sendError(req, "503 Service Unavailable", "no telemetry from the Nano yet");
  if (!t.hasMasks) return sendError(req, "501 Not Implemented", "Nano firmware has no sensor switches; re-flash it");
  JsonDocument doc;
  writeSensorState(doc, t);
  return sendJson(req, doc);
}

// POST {"mq5":false} | {"switches":{"mq5":false,"dht11":true}} | {"all":false}  (all off = video-only mode)
static esp_err_t sensorsPostHandler(httpd_req_t *req) {
  JsonDocument body;
  if (!readJsonBody(req, body)) return sendError(req, "400 Bad Request", "invalid JSON body");
  NanoTelemetry t = nano.latest();
  if (!t.valid) return sendError(req, "503 Service Unavailable", "no telemetry from the Nano yet");
  if (!t.hasMasks) return sendError(req, "501 Not Implemented", "Nano firmware has no sensor switches; re-flash it");

  // The Nano applies a command within one telemetry period (0.5 s); answer with the state that was asked for.
  JsonDocument doc;
  doc["ok"] = true;
  if (body["all"].is<bool>()) {
    bool on = body["all"].as<bool>();
    nano.setSensor("all", on);
    writeSensorState(doc, t, -1, on, true);
  } else {
    JsonObject req_ = body["switches"].is<JsonObject>() ? body["switches"].as<JsonObject>() : body.as<JsonObject>();
    uint8_t sentMask = 0, wantedMask = 0;
    for (JsonPair kv : req_) {
      int i = sensorIndex(kv.key().c_str());
      if (i < 0 || !kv.value().is<bool>()) return sendError(req, "400 Bad Request", "expected {sensor: true|false}");
      if (!t.fitted(i)) return sendError(req, "400 Bad Request", "that sensor is not fitted");
      sentMask |= 1 << i;
      if (kv.value().as<bool>()) wantedMask |= 1 << i;
      nano.setSensor(NANO_NAMES[i], kv.value().as<bool>());
    }
    if (!sentMask) return sendError(req, "400 Bad Request", "expected {sensor: true|false}, {switches: {...}} or {all: bool}");
    NanoTelemetry merged = t;
    merged.enabled = (t.enabled & ~sentMask) | wantedMask;
    merged.warm = t.warm & ~(sentMask & wantedMask & ~t.enabled);  // newly switched-on sensors start warming
    writeSensorState(doc, merged);
  }
  pushNow = true;
  return sendJson(req, doc);
}

static esp_err_t configHandler(httpd_req_t *req) {
  JsonDocument body;
  if (!readJsonBody(req, body)) return sendError(req, "400 Bad Request", "invalid JSON body");
  if (!body["obstacle_cm"].isNull()) nano.configure("OBS", body["obstacle_cm"].as<int>());
  if (!body["max_pwm"].isNull()) nano.configure("MAX", body["max_pwm"].as<int>());
  JsonDocument doc;
  doc["ok"] = true;
  return sendJson(req, doc);
}

static esp_err_t sendFrame(httpd_req_t *req, camera_fb_t *fb) {
  httpd_resp_set_type(req, "image/jpeg");
  httpd_resp_set_hdr(req, "Content-Disposition", "inline; filename=capture.jpg");
  addCors(req);
  return httpd_resp_send(req, (const char *)fb->buf, fb->len);
}

static esp_err_t captureHandler(httpd_req_t *req) {
  if (!cameraOk) return sendError(req, "503 Service Unavailable", "camera not available");
  if (xSemaphoreTake(camMutex, pdMS_TO_TICKS(3000)) != pdTRUE) {
    return sendError(req, "503 Service Unavailable", "camera busy");
  }
  bool flash = queryFlag(req, "flash");
  sensor_t *s = esp_camera_sensor_get();
  s->set_framesize(s, captureFramesize);
  s->set_quality(s, CAPTURE_JPEG_QUALITY);
  if (flash) digitalWrite(FLASH_LED_PIN, HIGH);
  // Drop frames already buffered at the stream resolution.
  for (int i = 0; i < 2; i++) {
    camera_fb_t *stale = esp_camera_fb_get();
    if (stale) esp_camera_fb_return(stale);
  }
  camera_fb_t *fb = esp_camera_fb_get();
  if (flash) digitalWrite(FLASH_LED_PIN, LOW);
  esp_err_t res;
  if (fb) {
    res = sendFrame(req, fb);
    esp_camera_fb_return(fb);
  } else {
    res = sendError(req, "500 Internal Server Error", "capture failed");
  }
  s->set_framesize(s, STREAM_FRAMESIZE);
  s->set_quality(s, STREAM_JPEG_QUALITY);
  xSemaphoreGive(camMutex);
  return res;
}

static esp_err_t snapshotHandler(httpd_req_t *req) {
  if (!cameraOk) return sendError(req, "503 Service Unavailable", "camera not available");
  if (xSemaphoreTake(camMutex, pdMS_TO_TICKS(2000)) != pdTRUE) return sendError(req, "503 Service Unavailable", "camera busy");
  camera_fb_t *fb = esp_camera_fb_get();
  esp_err_t res = fb ? sendFrame(req, fb) : sendError(req, "500 Internal Server Error", "capture failed");
  if (fb) esp_camera_fb_return(fb);
  xSemaphoreGive(camMutex);
  return res;
}

#define PART_BOUNDARY "gaiaframeboundary"
static const char *STREAM_CONTENT_TYPE = "multipart/x-mixed-replace;boundary=" PART_BOUNDARY;
static const char *STREAM_BOUNDARY = "\r\n--" PART_BOUNDARY "\r\n";
static const char *STREAM_PART = "Content-Type: image/jpeg\r\nContent-Length: %u\r\n\r\n";

static esp_err_t streamHandler(httpd_req_t *req) {
  if (!cameraOk) return sendError(req, "503 Service Unavailable", "camera not available");
  httpd_resp_set_type(req, STREAM_CONTENT_TYPE);
  addCors(req);
  char part[80];
  uint32_t lastFrame = 0;
  while (true) {
    uint32_t elapsed = millis() - lastFrame;
    if (elapsed < STREAM_MIN_FRAME_MS) delay(STREAM_MIN_FRAME_MS - elapsed);
    lastFrame = millis();
    if (xSemaphoreTake(camMutex, pdMS_TO_TICKS(3000)) != pdTRUE) continue;  // a capture is running
    camera_fb_t *fb = esp_camera_fb_get();
    if (!fb) {
      xSemaphoreGive(camMutex);
      delay(20);
      continue;
    }
    esp_err_t res = httpd_resp_send_chunk(req, STREAM_BOUNDARY, strlen(STREAM_BOUNDARY));
    if (res == ESP_OK) {
      size_t n = snprintf(part, sizeof(part), STREAM_PART, (unsigned)fb->len);
      res = httpd_resp_send_chunk(req, part, n);
    }
    if (res == ESP_OK) res = httpd_resp_send_chunk(req, (const char *)fb->buf, fb->len);
    esp_camera_fb_return(fb);
    xSemaphoreGive(camMutex);
    if (res != ESP_OK) break;  // client went away
  }
  return ESP_OK;
}

// ---------------------------------------------------------------- setup helpers

static void route(httpd_handle_t server, const char *uri, httpd_method_t method, esp_err_t (*handler)(httpd_req_t *)) {
  httpd_uri_t u = {};
  u.uri = uri;
  u.method = method;
  u.handler = handler;
  u.user_ctx = nullptr;
  httpd_register_uri_handler(server, &u);
}

void startServers() {
  httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
  cfg.server_port = 80;
  cfg.ctrl_port = 32768;
  cfg.max_uri_handlers = 20;
  cfg.stack_size = 8192;
  cfg.lru_purge_enable = true;
  if (httpd_start(&controlServer, &cfg) == ESP_OK) {
    route(controlServer, "/", HTTP_GET, rootHandler);
    route(controlServer, "/api/status", HTTP_GET, statusHandler);
    route(controlServer, "/api/telemetry", HTTP_GET, telemetryHandler);
    route(controlServer, "/api/drive", HTTP_POST, driveHandler);
    route(controlServer, "/api/stop", HTTP_POST, stopHandler);
    route(controlServer, "/api/arm", HTTP_POST, armHandler);
    route(controlServer, "/api/config", HTTP_POST, configHandler);
    route(controlServer, "/api/sensors", HTTP_GET, sensorsGetHandler);
    route(controlServer, "/api/sensors", HTTP_POST, sensorsPostHandler);
    for (const char *uri : {"/api/drive", "/api/stop", "/api/arm", "/api/config", "/api/sensors"}) {
      route(controlServer, uri, HTTP_OPTIONS, preflightHandler);
    }
    route(controlServer, "/capture", HTTP_GET, captureHandler);
    route(controlServer, "/snapshot", HTTP_GET, snapshotHandler);
  }
  httpd_config_t scfg = HTTPD_DEFAULT_CONFIG();
  scfg.server_port = 81;
  scfg.ctrl_port = 32769;
  scfg.stack_size = 8192;
  if (httpd_start(&streamServer, &scfg) == ESP_OK) {
    route(streamServer, "/stream", HTTP_GET, streamHandler);
  }
}

bool initCamera() {
  camera_config_t c = {};
  c.ledc_channel = LEDC_CHANNEL_0;
  c.ledc_timer = LEDC_TIMER_0;
  c.pin_d0 = Y2_GPIO_NUM;
  c.pin_d1 = Y3_GPIO_NUM;
  c.pin_d2 = Y4_GPIO_NUM;
  c.pin_d3 = Y5_GPIO_NUM;
  c.pin_d4 = Y6_GPIO_NUM;
  c.pin_d5 = Y7_GPIO_NUM;
  c.pin_d6 = Y8_GPIO_NUM;
  c.pin_d7 = Y9_GPIO_NUM;
  c.pin_xclk = XCLK_GPIO_NUM;
  c.pin_pclk = PCLK_GPIO_NUM;
  c.pin_vsync = VSYNC_GPIO_NUM;
  c.pin_href = HREF_GPIO_NUM;
  c.pin_sccb_sda = SIOD_GPIO_NUM;
  c.pin_sccb_scl = SIOC_GPIO_NUM;
  c.pin_pwdn = PWDN_GPIO_NUM;
  c.pin_reset = RESET_GPIO_NUM;
  c.xclk_freq_hz = 20000000;
  c.pixel_format = PIXFORMAT_JPEG;
  c.grab_mode = CAMERA_GRAB_LATEST;
  if (psramFound()) {
    // Allocate buffers for the largest (capture) size so resolution can switch at runtime.
    c.frame_size = CAPTURE_FRAMESIZE;
    c.jpeg_quality = CAPTURE_JPEG_QUALITY;
    c.fb_count = 2;
    c.fb_location = CAMERA_FB_IN_PSRAM;
  } else {
    captureFramesize = FRAMESIZE_SVGA;
    c.frame_size = FRAMESIZE_SVGA;
    c.jpeg_quality = 12;
    c.fb_count = 1;
    c.fb_location = CAMERA_FB_IN_DRAM;
  }
  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    Serial.printf("Camera init failed: 0x%x\n", err);
    return false;
  }
  sensor_t *s = esp_camera_sensor_get();
  s->set_framesize(s, STREAM_FRAMESIZE);
  s->set_quality(s, STREAM_JPEG_QUALITY);
  return true;
}

void startWifi() {
  WiFi.persistent(false);
  bool sta = false;
  if (GAIA_WIFI_MODE != GAIA_WIFI_AP_ONLY) {
    WiFi.mode(GAIA_WIFI_MODE == GAIA_WIFI_AP_STA ? WIFI_AP_STA : WIFI_STA);
    WiFi.setSleep(false);  // lower latency for driving and streaming
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED && millis() - start < WIFI_CONNECT_TIMEOUT_MS) delay(250);
    sta = staConnected();
  }
  bool ap = GAIA_WIFI_MODE != GAIA_WIFI_STA_FALLBACK_AP || !sta;
  if (ap) {
    if (!sta && GAIA_WIFI_MODE == GAIA_WIFI_STA_FALLBACK_AP) {
      WiFi.disconnect(true);
      WiFi.mode(WIFI_AP);
    } else if (GAIA_WIFI_MODE == GAIA_WIFI_AP_ONLY) {
      WiFi.mode(WIFI_AP);
    }
    WiFi.softAP(AP_SSID, AP_PASSWORD);
  }
  if (sta) {
    WiFi.setAutoReconnect(true);
    configTime(0, 0, "pool.ntp.org", "time.google.com");
    Serial.printf("Wi-Fi joined %s, IP %s\n", WIFI_SSID, WiFi.localIP().toString().c_str());
  }
  if (ap) Serial.printf("Hotspot %s up, IP %s\n", AP_SSID, WiFi.softAPIP().toString().c_str());
  wifiModeName = sta && ap ? "ap+sta" : sta ? "sta" : "ap";
  if (MDNS.begin(MDNS_NAME)) MDNS.addService("http", "tcp", 80);
}

// ---------------------------------------------------------------- telemetry task

void sampleReading() {
  NanoTelemetry t = nano.latest();
  if (!t.valid || millis() - t.receivedMs > 3000) return;  // nothing fresh to log
  if (qCount == QUEUE_CAPACITY) {  // drop the oldest when the backend has been away too long
    qHead = (qHead + 1) % QUEUE_CAPACITY;
    qCount = qCount - 1;
  }
  QueuedReading &r = queue[(qHead + qCount) % QUEUE_CAPACITY];
  r.uptimeMs = millis();
  r.epoch = timeSynced() ? (uint32_t)time(nullptr) : 0;
  r.t = t;
  qCount = qCount + 1;
}

// Every push also tells the backend which sensors are on, so a batch with no readings (all sensors off,
// video-only mode) still shows that the rover is connected.
void pushReadings() {
  if (strlen(BACKEND_URL) == 0 || !staConnected()) return;
  size_t n = qCount < PUSH_BATCH_MAX ? qCount : PUSH_BATCH_MAX;
  JsonDocument doc;
  doc["device_id"] = DEVICE_ID;
  doc["sent_uptime_ms"] = millis();
  NanoTelemetry now = nano.latest();
  if (now.valid && now.hasMasks) fillSwitches(doc["sensor_switches"].to<JsonObject>(), now);
  JsonArray readings = doc["readings"].to<JsonArray>();
  for (size_t i = 0; i < n; i++) {
    const QueuedReading &r = queue[(qHead + i) % QUEUE_CAPACITY];
    fillReading(readings.add<JsonObject>(), r.t, r.uptimeMs, r.epoch);
  }
  String body;
  serializeJson(doc, body);
  HTTPClient http;
  http.setTimeout(5000);
  http.begin(String(BACKEND_URL) + "/api/v1/telemetry");
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Key", DEVICE_KEY);
  int code = http.POST(body);
  http.end();
  lastPushOk = code >= 200 && code < 300;
  if (lastPushOk) {
    qHead = (qHead + n) % QUEUE_CAPACITY;
    qCount = qCount - n;
    pushIntervalMs = PUSH_PERIOD_MS;
  } else {
    Serial.printf("Telemetry push failed (HTTP %d), %u queued\n", code, (unsigned)qCount);
    pushIntervalMs = min<uint32_t>(pushIntervalMs * 2, 120000);  // back off while the backend is away
  }
}

void telemetryTask(void *) {
  uint32_t lastSample = 0, lastPush = 0;
  uint8_t lastEnabled = 0xFF;
  for (;;) {
    uint32_t now = millis();
    NanoTelemetry t = nano.latest();
    if (t.valid && t.hasMasks && t.enabled != lastEnabled) {  // switched from the app, the Nano console, or at boot
      if (lastEnabled != 0xFF) pushNow = true;
      lastEnabled = t.enabled;
    }
    if (now - lastSample >= SAMPLE_PERIOD_MS || pushNow) {
      lastSample = now;
      sampleReading();
    }
    if (now - lastPush >= pushIntervalMs || pushNow) {
      lastPush = now;
      pushNow = false;
      pushReadings();
    }
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

// ---------------------------------------------------------------- main

void setup() {
  Serial.begin(115200);
  Serial.println("\nGAIA rover " FIRMWARE_VERSION);
  pinMode(FLASH_LED_PIN, OUTPUT);
  digitalWrite(FLASH_LED_PIN, LOW);

  NanoSerial.setRxBufferSize(1024);
  nano.begin(NanoSerial, NANO_BAUD, NANO_RX_PIN, NANO_TX_PIN);
  nano.stop();

  camMutex = xSemaphoreCreateMutex();
  cameraOk = initCamera();
  startWifi();
  startServers();
  xTaskCreatePinnedToCore(telemetryTask, "telemetry", 10240, nullptr, 1, nullptr, 0);
}

void loop() {
  nano.poll();
  // Heartbeat watchdog: the app repeats drive commands every 100-150 ms while a control is held.
  if ((driveLeft || driveRight) && millis() - lastDriveMs > DRIVE_TIMEOUT_MS) {
    driveLeft = 0;
    driveRight = 0;
    nano.stop();
  }
  static uint32_t lastPing = 0;
  if (millis() - lastPing > 2000) {
    lastPing = millis();
    if (!nano.connected()) nano.ping();
  }
  delay(2);
}
