// GAIA rover - Arduino Nano controller configuration.
// Pin map and calibration live here so the sketch itself never needs editing.
#pragma once

// ---------------------------------------------------------------------------
// Link to the ESP32-CAM (hardware Serial on D0/D1).
// Disconnect the ESP32 wires from D0/D1 while uploading to the Nano over USB.
// ---------------------------------------------------------------------------
#define LINK_BAUD 57600

// ---------------------------------------------------------------------------
// Motor driver: 6 BO motors, skid steering. The three left motors are wired in
// parallel to one channel and the three right motors to the other.
//   DRIVER_MODE 0: L298N style  (IN1/IN2 direction pair + EN PWM per side)
//   DRIVER_MODE 1: DIR + PWM    (e.g. Cytron MDD10A: IN1 = DIR, EN = PWM, IN2 unused)
// ---------------------------------------------------------------------------
#define DRIVER_MODE 0
#define PIN_LEFT_EN   5   // PWM (Timer0)
#define PIN_LEFT_IN1  4
#define PIN_LEFT_IN2  7
#define PIN_RIGHT_EN  6   // PWM (Timer0)
#define PIN_RIGHT_IN1 8
#define PIN_RIGHT_IN2 11
#define INVERT_LEFT   false  // flip if a side drives backwards
#define INVERT_RIGHT  false

#define MIN_PWM 70            // BO motors stall below this; non-zero commands start here
#define DEFAULT_MAX_PWM 230   // leave headroom for the L298N voltage drop
#define RAMP_STEP 12          // PWM change per 20 ms tick (soft start, protects gearboxes)
#define FAILSAFE_MS 700       // stop if no drive command arrives for this long

// ---------------------------------------------------------------------------
// Sensors
// ---------------------------------------------------------------------------
#define PIN_DHT 2
#define PIN_MQ2 A0
// 1 only if an MQ2 module is wired to A0. Without one the pin floats; the Nano then reports -1 and
// the ESP32 leaves MQ2 out of the telemetry, so no noise ever reaches the knowledge store.
#define HAS_MQ2 0

// ---------------------------------------------------------------------------
// Per-sensor switches (the app can switch each sensor on or off to save battery)
//   HAS_*            1 if the sensor is fitted. Switch commands only work for fitted sensors.
//   DEFAULT_SENSORS_ON  1 = every fitted sensor starts switched on after power-up, 0 = all start off.
//   PIN_*_POWER      control pin of an ELECTRONIC SWITCH (logic-level MOSFET or relay module) in that
//                    sensor's supply line, -1 if there is none. Without one, a switched-off sensor is
//                    only ignored in software (no readings), so the MQ heater still draws current.
//                    Never power a sensor directly from a Nano pin: the MQ heaters need ~0.2 A each.
//   POWER_ACTIVE_HIGH  1 if the switch conducts when the pin is HIGH.
// ---------------------------------------------------------------------------
#define HAS_DHT 1
#define HAS_SOIL 1
#define HAS_MQ5 1
#define HAS_MQ6 1
#define DEFAULT_SENSORS_ON 1
#define PIN_DHT_POWER -1
#define PIN_SOIL_POWER -1
#define PIN_MQ5_POWER -1
#define PIN_MQ6_POWER -1
#define PIN_MQ2_POWER -1
#define POWER_ACTIVE_HIGH 1
#define PIN_MQ5 A1
#define PIN_MQ6 A2
#define PIN_SOIL A3
#define PIN_BATTERY A6        // analog-only pin, via divider
#define PIN_US_TRIG 9         // HC-SR04 (Servo library disables PWM on D9/D10; plain digital is fine)
#define PIN_US_ECHO 10
#define PIN_STATUS_LED 13

#define MQ_WARMUP_MS 60000UL  // heaters need ~1 min after power-on (24-48 h burn-in for new sensors)
#define DEFAULT_OBSTACLE_CM 20

// Capacitive soil probe calibration: read the raw value in dry air and in a glass of water.
#define SOIL_DRY_RAW 620
#define SOIL_WET_RAW 280

// Battery divider: R1 from battery+ to A6, R2 from A6 to GND.
#define BATTERY_R1_KOHM 10.0
#define BATTERY_R2_KOHM 4.7
#define ADC_REF_VOLTS 5.0

// ---------------------------------------------------------------------------
// Soil-probe arm (servo lowers the probe into the soil when parked)
// ---------------------------------------------------------------------------
#define PIN_ARM_SERVO 3
#define ARM_RETRACTED_DEG 10
#define ARM_DEPLOYED_DEG 115
#define ARM_STEP_DEG 2        // degrees per 15 ms step -> smooth motion
#define SOIL_SETTLE_MS 2000   // wait after lowering before readings are valid

// ---------------------------------------------------------------------------
// Timing
// ---------------------------------------------------------------------------
#define TELEMETRY_PERIOD_MS 500
#define DHT_PERIOD_MS 2000
#define ULTRASONIC_PERIOD_MS 60
