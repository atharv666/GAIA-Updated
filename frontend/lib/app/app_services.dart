import 'dart:typed_data';

import 'package:flutter/widgets.dart';

import '../core/config/app_config.dart';
import '../core/network/api_client.dart';
import '../services/connection_monitor.dart';
import '../services/diagnosis_repository.dart';
import '../services/farm_assistant_repository.dart';
import '../services/live_repository.dart';
import '../services/rover_repository.dart';
import '../services/sensor_repository.dart';
import '../services/telemetry_relay.dart';

/// A photo waiting to be diagnosed (rover still or phone camera).
class PendingCapture {
  const PendingCapture(this.bytes, {required this.source, this.filename = 'capture.jpg'});

  final Uint8List bytes;
  final String source; // rover | phone
  final String filename;
}

/// Everything the UI needs, created once at startup and provided through [GaiaScope].
class AppServices {
  AppServices._(this.config, this.api, this.rover)
      : sensors = SensorRepository(api),
        assistant = FarmAssistantRepository(api),
        live = LiveRepository(api),
        diagnosis = DiagnosisRepository(api),
        monitor = ConnectionMonitor(config, api, rover),
        relay = TelemetryRelay(config, rover, api);

  static Future<AppServices> create() async {
    final config = await AppConfig.load();
    final api = GaiaApiClient(config);
    return AppServices._(config, api, RoverRepository(config));
  }

  final AppConfig config;
  final GaiaApiClient api;
  final RoverRepository rover;
  final SensorRepository sensors;
  final FarmAssistantRepository assistant;
  final LiveRepository live;
  final DiagnosisRepository diagnosis;
  final ConnectionMonitor monitor;
  final TelemetryRelay relay;

  /// Rover/phone captures hand off to the Diagnose tab through this notifier.
  final ValueNotifier<PendingCapture?> pendingCapture = ValueNotifier(null);

  /// Requested bottom-navigation tab (e.g. jump to Diagnose after a capture).
  final ValueNotifier<int?> requestedTab = ValueNotifier(null);

  void sendToDiagnosis(PendingCapture capture, {required int diagnoseTab}) {
    pendingCapture.value = capture;
    requestedTab.value = diagnoseTab;
  }

  void dispose() {
    monitor.dispose();
    relay.dispose();
    pendingCapture.dispose();
    requestedTab.dispose();
  }
}

class GaiaScope extends InheritedWidget {
  const GaiaScope({super.key, required this.services, required super.child});

  final AppServices services;

  static AppServices of(BuildContext context) {
    final scope = context.dependOnInheritedWidgetOfExactType<GaiaScope>();
    assert(scope != null, 'GaiaScope missing above this widget');
    return scope!.services;
  }

  static AppServices? maybeOf(BuildContext context) => context.dependOnInheritedWidgetOfExactType<GaiaScope>()?.services;

  @override
  bool updateShouldNotify(GaiaScope oldWidget) => services != oldWidget.services;
}
