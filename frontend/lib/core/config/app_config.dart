import 'package:flutter/foundation.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// Connection settings, editable in the Settings sheet and persisted on device.
///
/// Typical setups:
/// * Rover joins the phone hotspot / farm Wi-Fi (recommended): set the rover URL to the IP
///   it prints on boot and the backend URL to the laptop/server running the GAIA backend.
/// * Phone joins the rover's own hotspot: rover URL `http://192.168.4.1`; enable
///   "Relay rover telemetry" so the app forwards readings to the backend.
class AppConfig extends ChangeNotifier {
  AppConfig._(this._prefs);

  static const _kBackend = 'gaia.backendUrl';
  static const _kRover = 'gaia.roverUrl';
  static const _kStream = 'gaia.roverStreamUrl';
  static const _kApiKey = 'gaia.apiKey';
  static const _kDeviceKey = 'gaia.deviceKey';
  static const _kRelay = 'gaia.relayTelemetry';
  static const _kSession = 'gaia.chatSession';

  /// Android emulator reaches the host machine at 10.0.2.2; browsers use localhost.
  static String get defaultBackendUrl => kIsWeb ? 'http://localhost:8000' : 'http://10.0.2.2:8000';

  /// Default IP of an ESP32 running its own soft-AP hotspot.
  static const defaultRoverUrl = 'http://192.168.4.1';

  final SharedPreferences _prefs;

  static Future<AppConfig> load() async => AppConfig._(await SharedPreferences.getInstance());

  String get backendUrl => _clean(_prefs.getString(_kBackend) ?? defaultBackendUrl);
  String get roverUrl => _clean(_prefs.getString(_kRover) ?? defaultRoverUrl);
  String get apiKey => _prefs.getString(_kApiKey) ?? '';
  String get deviceKey => _prefs.getString(_kDeviceKey) ?? 'gaia-rover-dev-key';
  bool get relayTelemetry => _prefs.getBool(_kRelay) ?? false;
  String? get chatSessionId => _prefs.getString(_kSession);

  /// The ESP32 streams MJPEG from a second server on port 81.
  String get roverStreamUrl {
    final custom = _prefs.getString(_kStream);
    if (custom != null && custom.trim().isNotEmpty) return custom.trim();
    final uri = Uri.tryParse(roverUrl);
    if (uri == null || uri.host.isEmpty) return '$roverUrl:81/stream';
    return uri.replace(port: 81, path: '/stream').toString();
  }

  String get customStreamUrl => _prefs.getString(_kStream) ?? '';

  Future<void> update({
    String? backendUrl,
    String? roverUrl,
    String? streamUrl,
    String? apiKey,
    String? deviceKey,
    bool? relayTelemetry,
  }) async {
    if (backendUrl != null) await _prefs.setString(_kBackend, _clean(backendUrl));
    if (roverUrl != null) await _prefs.setString(_kRover, _clean(roverUrl));
    if (streamUrl != null) await _prefs.setString(_kStream, streamUrl.trim());
    if (apiKey != null) await _prefs.setString(_kApiKey, apiKey.trim());
    if (deviceKey != null) await _prefs.setString(_kDeviceKey, deviceKey.trim());
    if (relayTelemetry != null) await _prefs.setBool(_kRelay, relayTelemetry);
    notifyListeners();
  }

  Future<void> setChatSession(String? id) async {
    if (id == null) {
      await _prefs.remove(_kSession);
    } else {
      await _prefs.setString(_kSession, id);
    }
  }

  static String _clean(String url) {
    var u = url.trim();
    if (u.isEmpty) return u;
    if (!u.startsWith('http://') && !u.startsWith('https://')) u = 'http://$u';
    while (u.endsWith('/')) {
      u = u.substring(0, u.length - 1);
    }
    return u;
  }
}
