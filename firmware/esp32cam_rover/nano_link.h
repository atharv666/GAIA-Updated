// Checksummed UART line protocol to the Arduino Nano (see firmware/PROTOCOL.md).
#pragma once

#include <Arduino.h>

enum NanoFlags : uint8_t {
  NANO_MOVING = 1 << 0,
  NANO_OBSTACLE = 1 << 1,
  NANO_FAILSAFE = 1 << 2,
  NANO_DHT_OK = 1 << 3,
  NANO_MQ_WARM = 1 << 4,
  NANO_SOIL_VALID = 1 << 5,
};

// Sensors that can be switched on and off. Bit order matches the inst / en / warm masks in the Nano frame.
static const int SENSOR_COUNT = 5;
static const char *const SENSOR_KEYS[SENSOR_COUNT] = {"dht11", "soil_moisture", "mq5", "mq6", "mq2"};  // backend / app
static const char *const NANO_NAMES[SENSOR_COUNT] = {"dht11", "soil", "mq5", "mq6", "mq2"};            // UART protocol

inline int sensorIndex(const char *key) {
  for (int i = 0; i < SENSOR_COUNT; i++) {
    if (!strcmp(key, SENSOR_KEYS[i])) return i;
  }
  return -1;
}

struct NanoTelemetry {
  bool valid = false;
  uint32_t receivedMs = 0;
  uint32_t seq = 0;
  int tempX10 = -9999;
  int humX10 = -9999;
  int mq2 = -1, mq5 = -1, mq6 = -1;
  int soilX10 = -1;
  int distanceCm = -1;
  uint8_t arm = 0;
  int batteryMv = 0;
  uint8_t flags = 0;
  // Sensor switches. Older Nano firmware sends no masks: then everything fitted counts as on and warm.
  bool hasMasks = false;
  uint8_t installed = 0x0F, enabled = 0x0F, warm = 0x0F;

  bool has(NanoFlags f) const { return flags & f; }
  bool fitted(int i) const { return installed >> i & 1; }
  bool on(int i) const { return enabled >> i & 1; }
  bool warmed(int i) const { return warm >> i & 1; }
};

inline const char *armName(uint8_t arm) {
  static const char *names[] = {"retracted", "deploying", "settling", "deployed", "retracting"};
  return arm < 5 ? names[arm] : "unknown";
}

class NanoLink {
 public:
  void begin(HardwareSerial &port, uint32_t baud, int rxPin, int txPin) {
    port_ = &port;
    lock_ = xSemaphoreCreateMutex();
    port_->begin(baud, SERIAL_8N1, rxPin, txPin);
  }

  // Call often from loop(): reads and parses complete lines.
  void poll() {
    while (port_ && port_->available()) {
      char c = (char)port_->read();
      if (c == '\n' || c == '\r') {
        if (len_) {
          buf_[len_] = '\0';
          handleLine(buf_);
          len_ = 0;
        }
      } else if (len_ < sizeof(buf_) - 1) {
        buf_[len_++] = c;
      } else {
        len_ = 0;
      }
    }
  }

  void drive(int left, int right) {
    char body[24];
    snprintf(body, sizeof(body), "D,%d,%d", constrain(left, -255, 255), constrain(right, -255, 255));
    send(body);
  }
  void stop() { send("S"); }
  void arm(bool deploy) { send(deploy ? "A,1" : "A,0"); }
  void ping() { send("P"); }
  // name: one of NANO_NAMES, or "all"
  void setSensor(const char *name, bool on) {
    char body[24];
    snprintf(body, sizeof(body), "W,%s,%d", name, on ? 1 : 0);
    send(body);
  }
  void configure(const char *key, int value) {
    char body[24];
    snprintf(body, sizeof(body), "C,%s,%d", key, value);
    send(body);
  }

  NanoTelemetry latest() {
    NanoTelemetry copy;
    xSemaphoreTake(lock_, portMAX_DELAY);
    copy = latest_;
    xSemaphoreGive(lock_);
    return copy;
  }

  bool connected(uint32_t withinMs = 3000) {
    NanoTelemetry t = latest();
    return t.valid && millis() - t.receivedMs < withinMs;
  }

  uint32_t lastRejectMs() const { return lastRejectMs_; }

 private:
  static uint8_t checksum(const char *s) {
    uint8_t cs = 0;
    while (*s) cs ^= (uint8_t)*s++;
    return cs;
  }

  void send(const char *body) {
    char out[48];
    snprintf(out, sizeof(out), "$%s*%02X\n", body, checksum(body));
    xSemaphoreTake(lock_, portMAX_DELAY);
    port_->print(out);
    xSemaphoreGive(lock_);
  }

  void handleLine(char *line) {
    if (line[0] != '$') return;
    char *star = strrchr(line, '*');
    if (!star) return;
    *star = '\0';
    char *body = line + 1;
    if (checksum(body) != (uint8_t)strtol(star + 1, nullptr, 16)) return;

    if (body[0] == 'T') {
      NanoTelemetry t;
      unsigned long seq;
      int arm, flags, inst = 0, en = 0, warm = 0;
      int n = sscanf(body, "T,%lu,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d,%d", &seq, &t.tempX10, &t.humX10, &t.mq2,
                     &t.mq5, &t.mq6, &t.soilX10, &t.distanceCm, &arm, &t.batteryMv, &flags, &inst, &en, &warm);
      if (n != 11 && n != 14) return;  // 11: Nano firmware without sensor switches
      t.seq = seq;
      t.arm = (uint8_t)arm;
      t.flags = (uint8_t)flags;
      if (n == 14) {
        t.hasMasks = true;
        t.installed = (uint8_t)inst;
        t.enabled = (uint8_t)en;
        t.warm = (uint8_t)warm;
      }
      t.valid = true;
      t.receivedMs = millis();
      xSemaphoreTake(lock_, portMAX_DELAY);
      latest_ = t;
      xSemaphoreGive(lock_);
    } else if (body[0] == 'E') {
      lastRejectMs_ = millis();
    }
  }

  HardwareSerial *port_ = nullptr;
  SemaphoreHandle_t lock_ = nullptr;
  char buf_[96];
  size_t len_ = 0;
  NanoTelemetry latest_;
  volatile uint32_t lastRejectMs_ = 0;
};
