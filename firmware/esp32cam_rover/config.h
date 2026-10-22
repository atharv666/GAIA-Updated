// GAIA rover - ESP32-CAM configuration. Edit before flashing.
#pragma once

// ---------------------------------------------------------------------------
// Identity
// ---------------------------------------------------------------------------
#define DEVICE_ID "rover-01"
#define MDNS_NAME "gaia-rover"  // reachable as http://gaia-rover.local

// ---------------------------------------------------------------------------
// Wi-Fi
//   GAIA_WIFI_STA_FALLBACK_AP : join WIFI_SSID (farm router / phone hotspot); if that fails,
//                               start the rover's own hotspot (phone connects directly).
//   GAIA_WIFI_AP_ONLY         : always run the rover hotspot (the app relays telemetry).
//   GAIA_WIFI_AP_STA          : hotspot for the phone AND join WIFI_SSID for the backend.
// ---------------------------------------------------------------------------
#define GAIA_WIFI_STA_FALLBACK_AP 0
#define GAIA_WIFI_AP_ONLY 1
#define GAIA_WIFI_AP_STA 2
#define GAIA_WIFI_MODE GAIA_WIFI_STA_FALLBACK_AP

#define WIFI_SSID "YOUR_WIFI_OR_PHONE_HOTSPOT"
#define WIFI_PASSWORD "YOUR_PASSWORD"
#define WIFI_CONNECT_TIMEOUT_MS 15000

#define AP_SSID "GAIA-Rover"
#define AP_PASSWORD "gaia-rover-1234"  // 8+ characters

// ---------------------------------------------------------------------------
// Backend (telemetry push). Leave BACKEND_URL empty to disable pushing
// (for example when only the phone app relays telemetry).
// ---------------------------------------------------------------------------
#define BACKEND_URL "http://192.168.1.10:8000"
#define DEVICE_KEY "gaia-rover-dev-key"  // must match DEVICE_KEY in backend/.env
#define SAMPLE_PERIOD_MS 5000            // one reading queued every 5 s
#define PUSH_PERIOD_MS 15000             // batch upload every 15 s
#define QUEUE_CAPACITY 360               // ~30 min of readings kept while offline
#define PUSH_BATCH_MAX 60

// ---------------------------------------------------------------------------
// Link to the Arduino Nano (UART1 on free ESP32-CAM pins; SD card unused).
//   ESP32 GPIO14 (TX) -> Nano D0 (RX)
//   Nano D1 (TX) -> 1k/2k voltage divider -> ESP32 GPIO13 (RX)   (5 V -> 3.3 V)
// Avoid GPIO12 for RX: it is a boot strapping pin.
// ---------------------------------------------------------------------------
#define NANO_RX_PIN 13
#define NANO_TX_PIN 14
#define NANO_BAUD 57600
#define DRIVE_TIMEOUT_MS 500  // stop if the app's heartbeat stops (Nano has its own 700 ms failsafe)

// ---------------------------------------------------------------------------
// Camera
// ---------------------------------------------------------------------------
#define STREAM_FRAMESIZE FRAMESIZE_VGA    // 640x480 live view
#define STREAM_JPEG_QUALITY 14            // 10 (best) .. 63
#define STREAM_MIN_FRAME_MS 70            // cap ~14 fps to leave Wi-Fi headroom
#define CAPTURE_FRAMESIZE FRAMESIZE_UXGA  // 1600x1200 stills for diagnosis
#define CAPTURE_JPEG_QUALITY 10
#define FLASH_LED_PIN 4
