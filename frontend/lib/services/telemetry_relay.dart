import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/config/app_config.dart';
import '../core/network/api_client.dart';
import 'rover_repository.dart';

/// Store-and-forward relay for when the phone is on the rover's own hotspot and the rover
/// cannot reach the backend itself: poll `/api/telemetry` and forward to `/api/v1/telemetry`.
class TelemetryRelay extends ChangeNotifier {
  TelemetryRelay(this._config, this._rover, this._api) {
    _config.addListener(_sync);
    _sync();
  }

  static const pollEvery = Duration(seconds: 5);
  static const maxQueue = 720; // one hour at 5 s

  final AppConfig _config;
  final RoverRepository _rover;
  final GaiaApiClient _api;
  final List<Map<String, dynamic>> _queue = [];
  Timer? _timer;
  int forwarded = 0;
  String _deviceId = 'rover-01';
  Map<String, dynamic>? _switches; // which sensors are on: tells the backend 'off on purpose' from 'no data'
  String? lastError;

  bool get running => _timer != null;
  int get queued => _queue.length;

  void _sync() {
    if (_config.relayTelemetry && _timer == null) {
      _timer = Timer.periodic(pollEvery, (_) => _tick());
    } else if (!_config.relayTelemetry && _timer != null) {
      _timer?.cancel();
      _timer = null;
    }
    notifyListeners();
  }

  Future<void> _tick() async {
    try {
      final t = await _rover.telemetry();
      _deviceId = (t['device_id'] ?? _deviceId).toString();
      final sw = (t['state'] is Map) ? (t['state'] as Map)['sensor_switches'] : null;
      if (sw is Map) _switches = Map<String, dynamic>.from(sw);
      _queue.add({
        'ts': DateTime.now().toUtc().toIso8601String(), // phone clock: the rover may not have NTP
        'values': t['values'] ?? const {},
        if (t['state'] != null) 'state': t['state'],
      });
      if (_queue.length > maxQueue) _queue.removeRange(0, _queue.length - maxQueue);
    } on ApiException catch (e) {
      lastError = e.message;
    }
    if (_queue.isNotEmpty) {
      final batch = List<Map<String, dynamic>>.from(_queue);
      try {
        await _api.postJson('/telemetry', {'device_id': _deviceId, 'readings': batch, if (_switches != null) 'sensor_switches': _switches},
            headers: {'X-Device-Key': _config.deviceKey}, timeout: const Duration(seconds: 10));
        _queue.removeRange(0, batch.length);
        forwarded += batch.length;
        lastError = null;
      } on ApiException catch (e) {
        lastError = 'Backend: ${e.message}';
      }
    }
    notifyListeners();
  }

  @override
  void dispose() {
    _timer?.cancel();
    _config.removeListener(_sync);
    super.dispose();
  }
}
