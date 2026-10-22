import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:http/http.dart' as http;

import '../core/config/app_config.dart';
import '../core/network/api_client.dart';
import '../models/json_utils.dart';
import '../models/rover_models.dart';
import '../models/sensor_switches.dart';

/// Direct HTTP link to the ESP32-CAM rover (firmware/PROTOCOL.md).
class RoverRepository {
  RoverRepository(this.config, {http.Client? client}) : _http = client ?? http.Client();

  final AppConfig config;
  final http.Client _http; // one keep-alive client keeps drive latency low

  Uri _uri(String path) => Uri.parse('${config.roverUrl}$path');
  String get streamUrl => config.roverStreamUrl;
  String get snapshotUrl => '${config.roverUrl}/snapshot';

  Future<dynamic> _get(String path, {Duration timeout = const Duration(seconds: 4)}) async {
    try {
      final r = await _http.get(_uri(path)).timeout(timeout);
      if (r.statusCode >= 400) throw ApiException('Rover returned HTTP ${r.statusCode}', statusCode: r.statusCode);
      return jsonDecode(utf8.decode(r.bodyBytes));
    } on ApiException {
      rethrow;
    } catch (_) {
      throw ApiException('Rover not reachable at ${config.roverUrl}');
    }
  }

  Future<dynamic> _post(String path, [Object? body]) async {
    try {
      final r = await _http
          .post(_uri(path), headers: {'Content-Type': 'application/json'}, body: jsonEncode(body ?? const {}))
          .timeout(const Duration(seconds: 3));
      final data = r.body.isEmpty ? null : jsonDecode(utf8.decode(r.bodyBytes));
      if (r.statusCode >= 400) {
        throw ApiException(str(map(data)['error'], 'Rover returned HTTP ${r.statusCode}'), statusCode: r.statusCode);
      }
      return data;
    } on ApiException {
      rethrow;
    } catch (_) {
      throw ApiException('Rover not reachable at ${config.roverUrl}');
    }
  }

  Future<RoverStatus> status() async => RoverStatus.fromJson(map(await _get('/api/status')));

  Future<Map<String, dynamic>> telemetry() async => map(await _get('/api/telemetry'));

  Future<Map<String, dynamic>> drive({String? dir, int speed = 180, double? throttle, double? steer}) async {
    final body = dir != null ? {'dir': dir, 'speed': speed} : {'throttle': throttle ?? 0, 'steer': steer ?? 0};
    return map(await _post('/api/drive', body));
  }

  Future<void> stop() => _post('/api/stop');

  /// Which sensors are switched on (and which MQ heaters are still warming up).
  Future<RoverSensors> sensors() async => RoverSensors.fromJson(map(await _get('/api/sensors')));

  /// Switches sensors on or off: `{'mq5': false}`. All off is video-only mode.
  Future<RoverSensors> setSensors(Map<String, bool> switches) async =>
      RoverSensors.fromJson(map(await _post('/api/sensors', {'switches': switches})));

  Future<Map<String, dynamic>> arm({required bool deploy}) async =>
      map(await _post('/api/arm', {'action': deploy ? 'deploy' : 'retract'}));

  /// High-resolution still for Pipeline B.
  Future<Uint8List> captureStill({bool flash = false}) async {
    try {
      final r = await _http.get(_uri('/capture${flash ? '?flash=1' : ''}')).timeout(const Duration(seconds: 15));
      if (r.statusCode != 200 || !(r.headers['content-type'] ?? '').startsWith('image/')) {
        throw ApiException('Rover capture failed (HTTP ${r.statusCode})', statusCode: r.statusCode);
      }
      return r.bodyBytes;
    } on ApiException {
      rethrow;
    } catch (_) {
      throw ApiException('Rover not reachable at ${config.roverUrl}');
    }
  }
}

/// Press-and-hold driving: repeats the command as a heartbeat (the rover stops on its own
/// if commands stop arriving, e.g. when the phone leaves Wi-Fi range).
class DriveController {
  DriveController(this._rover);

  static const heartbeat = Duration(milliseconds: 120);

  final RoverRepository _rover;
  final ValueNotifier<String?> lastError = ValueNotifier(null);
  final ValueNotifier<String> state = ValueNotifier('parked');
  Timer? _timer;
  Map<String, Object?>? _command;
  bool _inFlight = false;
  int speed = 180;

  void hold(String dir) => _start({'dir': dir}, dir);

  void joystick(double throttle, double steer) {
    if (throttle.abs() < 0.08 && steer.abs() < 0.08) {
      release();
      return;
    }
    _command = {'throttle': throttle, 'steer': steer};
    state.value = 'joystick';
    _timer ??= Timer.periodic(heartbeat, (_) => _send());
  }

  void _start(Map<String, Object?> command, String label) {
    _command = command;
    state.value = label;
    _send();
    _timer?.cancel();
    _timer = Timer.periodic(heartbeat, (_) => _send());
  }

  Future<void> _send() async {
    final c = _command;
    if (c == null || _inFlight) return; // never queue up stale commands
    _inFlight = true;
    try {
      final r = c.containsKey('dir')
          ? await _rover.drive(dir: c['dir'] as String, speed: speed)
          : await _rover.drive(throttle: c['throttle'] as double, steer: c['steer'] as double);
      lastError.value = r['held'] != null ? 'Raising the soil probe before moving…' : (r['blocked'] != null ? 'Obstacle ahead' : null);
    } on ApiException catch (e) {
      lastError.value = e.message;
    } finally {
      _inFlight = false;
    }
  }

  Future<void> release() async {
    _timer?.cancel();
    _timer = null;
    final wasDriving = _command != null;
    _command = null;
    state.value = 'parked';
    if (wasDriving) {
      try {
        await _rover.stop();
      } on ApiException catch (e) {
        lastError.value = e.message;
      }
    }
  }

  void dispose() {
    release();
    lastError.dispose();
    state.dispose();
  }
}
