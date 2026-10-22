import 'dart:async';

import 'package:flutter/foundation.dart';

import '../core/config/app_config.dart';
import '../core/network/api_client.dart';
import '../models/json_utils.dart';
import '../models/rover_models.dart';
import 'rover_repository.dart';

/// Periodically checks the backend and the rover so every screen can show live status.
class ConnectionMonitor extends ChangeNotifier {
  ConnectionMonitor(this._config, this._api, this._rover) {
    _config.addListener(refresh);
    refresh();
    _timer = Timer.periodic(const Duration(seconds: 10), (_) => refresh());
  }

  final AppConfig _config;
  final GaiaApiClient _api;
  final RoverRepository _rover;
  Timer? _timer;

  bool? backendOnline;
  bool llmReady = false;
  bool vlmReady = false;
  String? backendError;
  RoverStatus? rover;
  String? roverError;

  bool get roverOnline => rover != null;

  Future<void> refresh() async {
    await Future.wait([_checkBackend(), _checkRover()]);
    notifyListeners();
  }

  Future<void> _checkBackend() async {
    try {
      final h = map(await _api.getJson('/health', timeout: const Duration(seconds: 6)));
      backendOnline = true;
      llmReady = boolean(map(h['llm'])['available']);
      vlmReady = boolean(map(h['vlm'])['available']);
      backendError = null;
    } on ApiException catch (e) {
      backendOnline = false;
      backendError = e.message;
    }
  }

  Future<void> _checkRover() async {
    try {
      rover = await _rover.status();
      roverError = null;
    } on ApiException catch (e) {
      rover = null;
      roverError = e.message;
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    _config.removeListener(refresh);
    super.dispose();
  }
}
