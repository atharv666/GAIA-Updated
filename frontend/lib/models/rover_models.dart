import 'json_utils.dart';

/// `GET /api/status` from the ESP32-CAM (or the simulator).
class RoverStatus {
  const RoverStatus({
    required this.deviceId,
    required this.firmware,
    required this.uptime,
    required this.wifiMode,
    required this.ssid,
    required this.ip,
    required this.nanoConnected,
    required this.cameraOk,
    required this.arm,
    required this.left,
    required this.right,
    this.rssi,
    this.obstacleCm,
    this.batteryV,
    this.backendPushOk,
    this.queued = 0,
  });

  factory RoverStatus.fromJson(Map<String, dynamic> j) {
    final wifi = map(j['wifi']);
    final drive = map(j['drive']);
    final backend = map(j['backend']);
    return RoverStatus(
      deviceId: str(j['device_id'], 'rover'),
      firmware: str(j['firmware']),
      uptime: Duration(milliseconds: integer(j['uptime_ms'])),
      wifiMode: str(wifi['mode']),
      ssid: str(wifi['ssid']),
      ip: str(wifi['ip']),
      rssi: wifi['rssi'] == null ? null : integer(wifi['rssi']),
      nanoConnected: boolean(map(j['nano'])['connected']),
      cameraOk: boolean(map(j['camera'])['ok'], true),
      arm: str(j['arm'], 'unknown'),
      left: integer(drive['left']),
      right: integer(drive['right']),
      obstacleCm: j['obstacle_cm'] == null ? null : integer(j['obstacle_cm']),
      batteryV: dblOrNull(j['battery_v']),
      backendPushOk: backend['last_push_ok'] is bool ? backend['last_push_ok'] as bool : null,
      queued: integer(backend['queued']),
    );
  }

  final String deviceId;
  final String firmware;
  final Duration uptime;
  final String wifiMode;
  final String ssid;
  final String ip;
  final int? rssi;
  final bool nanoConnected;
  final bool cameraOk;
  final String arm;
  final int left;
  final int right;
  final int? obstacleCm;
  final double? batteryV;
  final bool? backendPushOk;
  final int queued;

  bool get moving => left != 0 || right != 0;
  bool get armDeployed => arm == 'deployed';
}
