// GAIA rover - Arduino Nano controller.
//
// Owns everything electrical below the ESP32-CAM: 6WD skid-steer motors, the soil-probe
// servo arm, DHT11, MQ5/MQ6 (MQ2 optional), capacitive soil probe, HC-SR04 and battery sensing.
// Talks to the ESP32-CAM over a checksummed line protocol (firmware/PROTOCOL.md).
//
// Every sensor can be switched on or off ("W,<name>,<0|1>"). A switched-off sensor produces no readings
// (and, with an electronic switch on PIN_*_POWER, draws no current). Switching an MQ sensor on restarts
// its heater warm-up: readings are withheld until MQ_WARMUP_MS has passed.
//
// Safety rules enforced here (independently of the ESP32 and the app):
//   * motors stop if no drive command arrives for FAILSAFE_MS
//   * forward motion stops when an obstacle is closer than the configured distance
//   * the soil arm is only lowered while stopped; any drive command retracts it first
//   * soil readings are reported only when the arm is deployed, settled and the rover is still
//
// Libraries: "DHT sensor library" + "Adafruit Unified Sensor" (Adafruit), Servo (built in).

#include <Servo.h>
#include <DHT.h>
#include "config.h"

enum ArmState : uint8_t { ARM_RETRACTED = 0, ARM_DEPLOYING = 1, ARM_SETTLING = 2, ARM_DEPLOYED = 3, ARM_RETRACTING = 4 };

enum Flags : uint8_t {
  FLAG_MOVING = 1 << 0,
  FLAG_OBSTACLE = 1 << 1,
  FLAG_FAILSAFE = 1 << 2,
  FLAG_DHT_OK = 1 << 3,
  FLAG_MQ_WARM = 1 << 4,
  FLAG_SOIL_VALID = 1 << 5,
};

// Sensors that can be switched. Bit order matches the inst / en / warm masks of the telemetry frame.
enum Sensor : uint8_t { S_DHT = 0, S_SOIL = 1, S_MQ5 = 2, S_MQ6 = 3, S_MQ2 = 4, S_COUNT = 5 };
const char *const SENSOR_NAMES[S_COUNT] = {"dht11", "soil", "mq5", "mq6", "mq2"};
const bool INSTALLED[S_COUNT] = {HAS_DHT, HAS_SOIL, HAS_MQ5, HAS_MQ6, HAS_MQ2};
const int8_t POWER_PINS[S_COUNT] = {PIN_DHT_POWER, PIN_SOIL_POWER, PIN_MQ5_POWER, PIN_MQ6_POWER, PIN_MQ2_POWER};

DHT dht(PIN_DHT, DHT11);
Servo armServo;

bool sensorOn[S_COUNT];
unsigned long sensorOnSinceMs[S_COUNT];  // when the sensor was last switched on (heater warm-up clock)

// Drive state
int targetLeft = 0, targetRight = 0;    // requested, -255..255
int currentLeft = 0, currentRight = 0;  // ramped output
unsigned long lastDriveCmdMs = 0;
unsigned long lastRampMs = 0;
bool failsafeTripped = false;
bool obstacleStop = false;
int maxPwm = DEFAULT_MAX_PWM;
int obstacleCm = DEFAULT_OBSTACLE_CM;

// Arm state
ArmState armState = ARM_RETRACTED;
int armAngle = ARM_RETRACTED_DEG;
unsigned long armStepMs = 0, armSettledSinceMs = 0;

// Sensor state
int tempX10 = -9999, humX10 = -9999;
bool dhtOk = false;
int distanceCm = -1;
unsigned long lastDhtMs = 0, lastUltrasonicMs = 0, lastTelemetryMs = 0;
unsigned long telemetrySeq = 0;

// Serial input
char lineBuf[64];
uint8_t lineLen = 0;

// ------------------------------------------------------------------ protocol

uint8_t installedMask() {
  uint8_t m = 0;
  for (uint8_t i = 0; i < S_COUNT; i++) if (INSTALLED[i]) m |= 1 << i;
  return m;
}

uint8_t enabledMask() {
  uint8_t m = 0;
  for (uint8_t i = 0; i < S_COUNT; i++) if (INSTALLED[i] && sensorOn[i]) m |= 1 << i;
  return m;
}

bool isMq(uint8_t s) { return s == S_MQ5 || s == S_MQ6 || s == S_MQ2; }

// Ready to produce a trustworthy reading: switched on, and for MQ sensors past the heater warm-up.
bool sensorReady(uint8_t s) {
  if (!INSTALLED[s] || !sensorOn[s]) return false;
  return !isMq(s) || millis() - sensorOnSinceMs[s] >= MQ_WARMUP_MS;
}

uint8_t warmMask() {
  uint8_t m = 0;
  for (uint8_t i = 0; i < S_COUNT; i++) if (INSTALLED[i] && sensorReady(i)) m |= 1 << i;
  return m;
}

uint8_t checksum(const char *s) {
  uint8_t cs = 0;
  while (*s) cs ^= (uint8_t)*s++;
  return cs;
}

void sendFrame(const char *body) {
  char out[112];
  snprintf(out, sizeof(out), "$%s*%02X\n", body, checksum(body));
  Serial.print(out);
}

void ack(const char *cmd, const char *info = nullptr) {
  char body[48];
  if (info) snprintf(body, sizeof(body), "K,%s,%s", cmd, info);
  else snprintf(body, sizeof(body), "K,%s", cmd);
  sendFrame(body);
}

void reject(const char *cmd, const char *reason) {
  char body[48];
  snprintf(body, sizeof(body), "E,%s,%s", cmd, reason);
  sendFrame(body);
}

// ------------------------------------------------------------------ motors

bool isMoving() { return currentLeft != 0 || currentRight != 0; }

void writeMotor(uint8_t en, uint8_t in1, uint8_t in2, int speed, bool invert) {
  if (invert) speed = -speed;
  int pwm = 0;
  if (speed != 0) pwm = map(abs(speed), 1, 255, MIN_PWM, maxPwm);
#if DRIVER_MODE == 0
  digitalWrite(in1, speed > 0 ? HIGH : LOW);
  digitalWrite(in2, speed < 0 ? HIGH : LOW);
#else
  digitalWrite(in1, speed >= 0 ? HIGH : LOW);  // DIR
  (void)in2;
#endif
  analogWrite(en, pwm);
}

void applyMotors() {
  writeMotor(PIN_LEFT_EN, PIN_LEFT_IN1, PIN_LEFT_IN2, currentLeft, INVERT_LEFT);
  writeMotor(PIN_RIGHT_EN, PIN_RIGHT_IN1, PIN_RIGHT_IN2, currentRight, INVERT_RIGHT);
}

void stopNow() {
  targetLeft = targetRight = currentLeft = currentRight = 0;
  applyMotors();
}

int rampToward(int current, int target) {
  int delta = constrain(target - current, -RAMP_STEP, RAMP_STEP);
  // Never ramp through a direction change: stop first, then reverse.
  if ((current > 0 && target < 0) || (current < 0 && target > 0)) delta = constrain(-current, -RAMP_STEP, RAMP_STEP);
  return current + delta;
}

void updateDrive() {
  unsigned long now = millis();
  if ((targetLeft || targetRight) && now - lastDriveCmdMs > FAILSAFE_MS) {
    targetLeft = targetRight = 0;
    failsafeTripped = true;
  }
  bool forward = targetLeft > 0 && targetRight > 0;
  obstacleStop = forward && obstacleCm > 0 && distanceCm > 0 && distanceCm < obstacleCm;
  int tl = obstacleStop ? 0 : targetLeft;
  int tr = obstacleStop ? 0 : targetRight;
  if (obstacleStop && currentLeft > 0 && currentRight > 0) {
    currentLeft = currentRight = 0;  // brake hard for obstacles instead of ramping
    applyMotors();
  }
  if (now - lastRampMs >= 20) {
    lastRampMs = now;
    currentLeft = rampToward(currentLeft, tl);
    currentRight = rampToward(currentRight, tr);
    applyMotors();
  }
}

void handleDrive(int left, int right) {
  left = constrain(left, -255, 255);
  right = constrain(right, -255, 255);
  lastDriveCmdMs = millis();
  failsafeTripped = false;
  if ((left || right) && armState != ARM_RETRACTED) {
    // Protect the probe: raise it before moving; the app's heartbeat resumes driving afterwards.
    if (armState != ARM_RETRACTING) armState = ARM_RETRACTING;
    targetLeft = targetRight = 0;
    reject("D", "arm");
    return;
  }
  targetLeft = left;
  targetRight = right;
}

// ------------------------------------------------------------------ soil arm

void updateArm() {
  unsigned long now = millis();
  if (now - armStepMs < 15) return;
  armStepMs = now;
  switch (armState) {
    case ARM_DEPLOYING:
      armAngle = min(armAngle + ARM_STEP_DEG, ARM_DEPLOYED_DEG);
      armServo.write(armAngle);
      if (armAngle == ARM_DEPLOYED_DEG) { armState = ARM_SETTLING; armSettledSinceMs = now; }
      break;
    case ARM_SETTLING:
      if (now - armSettledSinceMs >= SOIL_SETTLE_MS) armState = ARM_DEPLOYED;
      break;
    case ARM_RETRACTING:
      armAngle = max(armAngle - ARM_STEP_DEG, ARM_RETRACTED_DEG);
      armServo.write(armAngle);
      if (armAngle == ARM_RETRACTED_DEG) armState = ARM_RETRACTED;
      break;
    default:
      break;
  }
}

void handleArm(bool deploy) {
  if (deploy) {
    if (!sensorOn[S_SOIL]) { reject("A", "off"); return; }  // soil probe switched off: the servo stays still
    if (isMoving() || targetLeft || targetRight) { reject("A", "moving"); return; }
    if (armState == ARM_RETRACTED || armState == ARM_RETRACTING) armState = ARM_DEPLOYING;
  } else if (armState != ARM_RETRACTED) {
    armState = ARM_RETRACTING;
  }
  ack("A", deploy ? "1" : "0");
}

// ------------------------------------------------------------------ sensors

int readAveraged(uint8_t pin, uint8_t samples = 8) {
  long sum = 0;
  for (uint8_t i = 0; i < samples; i++) sum += analogRead(pin);
  return (int)(sum / samples);
}

// Switch one sensor. Returns false if it is not fitted.
bool setSensor(uint8_t s, bool on) {
  if (s >= S_COUNT || !INSTALLED[s]) return false;
  if (on && !sensorOn[s]) sensorOnSinceMs[s] = millis();  // restarts the MQ heater warm-up
  sensorOn[s] = on;
  if (POWER_PINS[s] >= 0) digitalWrite(POWER_PINS[s], (on == (POWER_ACTIVE_HIGH != 0)) ? HIGH : LOW);
  if (s == S_DHT) {
    dhtOk = false;
    lastDhtMs = millis() - DHT_PERIOD_MS;  // read soon after power-up (DHT11 needs about a second)
  }
  if (s == S_SOIL && !on && armState != ARM_RETRACTED) armState = ARM_RETRACTING;  // lift the probe out
  return true;
}

void readDht() {
  if (!sensorOn[S_DHT]) return;
  float t = dht.readTemperature();
  float h = dht.readHumidity();
  dhtOk = !(isnan(t) || isnan(h));
  if (dhtOk) {
    tempX10 = (int)round(t * 10);
    humX10 = (int)round(h * 10);
  }
}

void readUltrasonic() {
  digitalWrite(PIN_US_TRIG, LOW);
  delayMicroseconds(2);
  digitalWrite(PIN_US_TRIG, HIGH);
  delayMicroseconds(10);
  digitalWrite(PIN_US_TRIG, LOW);
  // 15 ms timeout (~2.5 m): nothing farther matters for stopping, and the short block means
  // the drive commands arriving meanwhile (~15 bytes every 100-150 ms) fit the 64-byte RX buffer.
  unsigned long echo = pulseIn(PIN_US_ECHO, HIGH, 15000UL);
  distanceCm = echo ? (int)(echo / 58UL) : -1;
}

bool soilValid() { return sensorOn[S_SOIL] && armState == ARM_DEPLOYED && !isMoving(); }

int soilPercentX10() {
  int raw = readAveraged(PIN_SOIL, 16);
  long pct = map(raw, SOIL_DRY_RAW, SOIL_WET_RAW, 0, 1000);
  return (int)constrain(pct, 0, 1000);
}

int batteryMillivolts() {
  float v = readAveraged(PIN_BATTERY) * ADC_REF_VOLTS / 1023.0 * (BATTERY_R1_KOHM + BATTERY_R2_KOHM) / BATTERY_R2_KOHM;
  return (int)(v * 1000);
}

void sendTelemetry() {
  uint8_t flags = 0;
  if (isMoving()) flags |= FLAG_MOVING;
  if (obstacleStop) flags |= FLAG_OBSTACLE;
  if (failsafeTripped) flags |= FLAG_FAILSAFE;
  bool dhtGood = sensorOn[S_DHT] && dhtOk;
  if (dhtGood) flags |= FLAG_DHT_OK;
  // All switched-on MQ sensors are warm (also true when none is on). Kept for older ESP32 firmware.
  bool mqWarm = true;
  for (uint8_t i = S_MQ5; i <= S_MQ2; i++) if (INSTALLED[i] && sensorOn[i] && !sensorReady(i)) mqWarm = false;
  if (mqWarm) flags |= FLAG_MQ_WARM;
  bool soil = soilValid();
  if (soil) flags |= FLAG_SOIL_VALID;

  // A sensor that is off (or still warming up) reports -1 / -9999: the ESP32 leaves it out of the readings.
  int mq2 = sensorReady(S_MQ2) ? readAveraged(PIN_MQ2) : -1;
  int mq5 = sensorReady(S_MQ5) ? readAveraged(PIN_MQ5) : -1;
  int mq6 = sensorReady(S_MQ6) ? readAveraged(PIN_MQ6) : -1;

  char body[96];
  snprintf(body, sizeof(body), "T,%lu,%d,%d,%d,%d,%d,%d,%d,%u,%d,%u,%u,%u,%u",
           telemetrySeq++, dhtGood ? tempX10 : -9999, dhtGood ? humX10 : -9999, mq2, mq5, mq6,
           soil ? soilPercentX10() : -1, distanceCm, (unsigned)armState, batteryMillivolts(), (unsigned)flags,
           (unsigned)installedMask(), (unsigned)enabledMask(), (unsigned)warmMask());
  sendFrame(body);
}

// ------------------------------------------------------------------ command parsing

void handleLine(char *line) {
  // Expect "$BODY*CS"
  if (line[0] != '$') return;
  char *star = strrchr(line, '*');
  if (!star || strlen(star) < 3) return;
  *star = '\0';
  char *body = line + 1;
  uint8_t expected = (uint8_t)strtol(star + 1, nullptr, 16);
  if (checksum(body) != expected) return;

  char *cmd = strtok(body, ",");
  if (!cmd) return;
  switch (cmd[0]) {
    case 'D': {
      char *l = strtok(nullptr, ",");
      char *r = strtok(nullptr, ",");
      if (l && r) handleDrive(atoi(l), atoi(r));
      break;
    }
    case 'S':
      lastDriveCmdMs = millis();
      stopNow();
      ack("S");
      break;
    case 'A': {
      char *v = strtok(nullptr, ",");
      if (v) handleArm(atoi(v) == 1);
      break;
    }
    case 'C': {
      char *key = strtok(nullptr, ",");
      char *val = strtok(nullptr, ",");
      if (!key || !val) { reject("C", "args"); break; }
      if (strcmp(key, "OBS") == 0) { obstacleCm = constrain(atoi(val), 0, 200); ack("C", "OBS"); }
      else if (strcmp(key, "MAX") == 0) { maxPwm = constrain(atoi(val), 60, 255); ack("C", "MAX"); }
      else reject("C", "key");
      break;
    }
    case 'W': {  // W,<dht11|soil|mq5|mq6|mq2|all>,<0|1>: switch a sensor
      char *name = strtok(nullptr, ",");
      char *v = strtok(nullptr, ",");
      if (!name || !v) { reject("W", "args"); break; }
      bool on = atoi(v) == 1;
      bool done = false;
      if (strcmp(name, "all") == 0) {
        for (uint8_t i = 0; i < S_COUNT; i++) done |= setSensor(i, on);
      } else {
        uint8_t i = 0;
        while (i < S_COUNT && strcmp(name, SENSOR_NAMES[i]) != 0) i++;
        if (i == S_COUNT) { reject("W", "name"); break; }
        if (!setSensor(i, on)) { reject("W", "notfitted"); break; }
        done = true;
      }
      if (done) {
        char info[16];
        snprintf(info, sizeof(info), "%s,%d", name, on ? 1 : 0);
        ack("W", info);
      } else {
        reject("W", "notfitted");
      }
      break;
    }
    case 'P':
      ack("P");
      break;
    default:
      reject(cmd, "unknown");
  }
}

void readLink() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (lineLen) {
        lineBuf[lineLen] = '\0';
        handleLine(lineBuf);
        lineLen = 0;
      }
    } else if (lineLen < sizeof(lineBuf) - 1) {
      lineBuf[lineLen++] = c;
    } else {
      lineLen = 0;  // overlong garbage: drop
    }
  }
}

// ------------------------------------------------------------------ main

void setup() {
  Serial.begin(LINK_BAUD);
  uint8_t outputs[] = {PIN_LEFT_EN, PIN_LEFT_IN1, PIN_LEFT_IN2, PIN_RIGHT_EN, PIN_RIGHT_IN1, PIN_RIGHT_IN2,
                       PIN_US_TRIG, PIN_STATUS_LED};
  for (uint8_t pin : outputs) pinMode(pin, OUTPUT);
  pinMode(PIN_US_ECHO, INPUT);
  for (uint8_t i = 0; i < S_COUNT; i++) {
    if (POWER_PINS[i] >= 0) pinMode(POWER_PINS[i], OUTPUT);
    sensorOn[i] = false;
    sensorOnSinceMs[i] = millis();
  }
  for (uint8_t i = 0; i < S_COUNT; i++) setSensor(i, INSTALLED[i] && DEFAULT_SENSORS_ON);
  stopNow();
  armServo.attach(PIN_ARM_SERVO);
  armServo.write(ARM_RETRACTED_DEG);
  dht.begin();
  sendFrame("K,BOOT");
}

void loop() {
  unsigned long now = millis();
  readLink();
  updateArm();
  if (now - lastUltrasonicMs >= ULTRASONIC_PERIOD_MS) {
    lastUltrasonicMs = now;
    readUltrasonic();
  }
  updateDrive();
  if (now - lastDhtMs >= DHT_PERIOD_MS) {
    lastDhtMs = now;
    readDht();
  }
  if (now - lastTelemetryMs >= TELEMETRY_PERIOD_MS) {
    lastTelemetryMs = now;
    sendTelemetry();
  }
  // LED: solid while driving, blinking when the failsafe tripped.
  digitalWrite(PIN_STATUS_LED, failsafeTripped ? ((now / 250) % 2) : isMoving());
}
