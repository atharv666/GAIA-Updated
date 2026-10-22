import '../core/network/api_client.dart';
import '../models/json_utils.dart';
import '../models/sensor_reading.dart';
import '../models/sensor_switches.dart';

/// Sensor log reads from the backend (the rover pushes readings; the app only displays them).
class SensorRepository {
  SensorRepository(this._api);

  final GaiaApiClient _api;

  Future<List<SensorReading>> latest() async {
    final data = map(await _api.getJson('/telemetry/latest'));
    return maps(data['readings']).map(SensorReading.fromJson).toList();
  }

  /// Which sensors are switched on or off (as reported by the rover) and whether it is connected.
  Future<SwitchState> switchState() async => SwitchState.fromJson(map(await _api.getJson('/telemetry/sensor-state')));

  Future<SensorSeries> history(String sensor, {int hours = 24}) async {
    final data = await _api.getJson('/telemetry/history', query: {'sensor': sensor, 'hours': '$hours'});
    return SensorSeries.fromJson(map(data));
  }

  Future<List<SensorSummary>> summary({String range = 'day:0'}) async {
    final data = map(await _api.getJson('/telemetry/summary', query: {'range': range}));
    return maps(data['stats']).map(SensorSummary.fromJson).toList();
  }

  Future<FarmOverview> overview() async => FarmOverview.fromJson(map(await _api.getJson('/overview')));

  /// Aggregates pending windows now (normally the backend does this every minute).
  Future<Map<String, dynamic>> sync() async => map(await _api.postJson('/ingest/sync', {'include_open': true}));
}
