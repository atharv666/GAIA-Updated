import 'json_utils.dart';

/// The sensors that can be switched on and off, in display order.
const kToggleOrder = ['dht11', 'soil_moisture', 'mq5', 'mq6', 'mq2'];

const _short = {'dht11': 'DHT11', 'soil_moisture': 'Soil probe', 'mq5': 'MQ5', 'mq6': 'MQ6', 'mq2': 'MQ2'};
const _long = {
  'dht11': 'DHT11 · temperature & humidity',
  'soil_moisture': 'Soil moisture probe',
  'mq5': 'MQ5 · methane / natural gas',
  'mq6': 'MQ6 · LPG / butane',
  'mq2': 'MQ2 · smoke / combustible gas',
};
const _readingKeys = {
  'dht11': ['temperature', 'humidity'],
  'soil_moisture': ['soil_moisture'],
  'mq5': ['mq5'],
  'mq6': ['mq6'],
  'mq2': ['mq2'],
};

String toggleShort(String toggle) => _short[toggle] ?? toggle;
String toggleLong(String toggle) => _long[toggle] ?? toggle;

/// The switch that powers a reading key (null for rover channels such as distance or battery).
String? toggleForSensor(String sensor) {
  for (final e in _readingKeys.entries) {
    if (e.value.contains(sensor)) return e.key;
  }
  return null;
}

/// One sensor switch as the backend knows it (`GET /telemetry/sensor-state`).
class SwitchInfo {
  const SwitchInfo({required this.toggle, required this.on, this.since});

  factory SwitchInfo.fromJson(Map<String, dynamic> j) =>
      SwitchInfo(toggle: str(j['toggle']), on: j['state'] == 'on', since: time(j['since']));

  final String toggle;
  final bool on;
  final DateTime? since;
}

class SwitchState {
  const SwitchState({this.deviceId, this.lastSeen, this.connected = false, this.switches = const []});

  factory SwitchState.fromJson(Map<String, dynamic> j) => SwitchState(
        deviceId: strOrNull(j['device_id']),
        lastSeen: time(j['last_seen']),
        connected: boolean(j['connected']),
        switches: maps(j['switches']).map(SwitchInfo.fromJson).toList(),
      );

  final String? deviceId;
  final DateTime? lastSeen;
  final bool connected;
  final List<SwitchInfo> switches;

  bool get isEmpty => switches.isEmpty;

  SwitchInfo? of(String toggle) {
    for (final s in switches) {
      if (s.toggle == toggle) return s;
    }
    return null;
  }

  /// Switches that are off right now.
  List<SwitchInfo> get off => switches.where((s) => !s.on).toList();

  /// Reading keys (temperature, mq5 ...) whose sensor is switched off.
  Set<String> get offSensors => {for (final s in off) ..._readingKeys[s.toggle] ?? const <String>[]};
}

/// Live state straight from the rover (`GET /api/sensors`): used to control the switches.
class RoverSensors {
  const RoverSensors({required this.switches, this.warming = const [], this.installed = const []});

  factory RoverSensors.fromJson(Map<String, dynamic> j) => RoverSensors(
        switches: map(j['switches']).map((k, v) => MapEntry(k, v == true)),
        warming: strings(j['warming']),
        installed: strings(j['installed']),
      );

  final Map<String, bool> switches;
  final List<String> warming;
  final List<String> installed;

  /// Fitted sensors in display order.
  List<String> get toggles => [
        for (final t in kToggleOrder)
          if (switches.containsKey(t)) t,
      ];

  bool get anyOn => switches.values.any((v) => v);

  RoverSensors copyWith({Map<String, bool>? switches}) =>
      RoverSensors(switches: switches ?? this.switches, warming: warming, installed: installed);
}
