import 'json_utils.dart';
import 'sensor_reading.dart';
import 'sensor_switches.dart';

/// One thing the system did, from `GET /live/stream` or `GET /live/state`.
class LiveEvent {
  const LiveEvent({required this.id, required this.at, required this.kind, required this.title, required this.data});

  factory LiveEvent.fromJson(Map<String, dynamic> j) => LiveEvent(
        id: integer(j['id']),
        at: time(j['ts']) ?? DateTime.now(),
        kind: str(j['kind']),
        title: str(j['title']),
        data: map(j['data']),
      );

  final int id;
  final DateTime at;
  final String kind;
  final String title;
  final Map<String, dynamic> data;

  /// Which box of the pipeline drawing lights up for this event.
  LiveStage? get stage => switch (kind) {
        'readings_received' => LiveStage.backend,
        'sensor_switched' => LiveStage.sensors,
        'window_closed' => LiveStage.window,
        'chunk_created' => LiveStage.chunk,
        'chunk_embedded' => LiveStage.embed,
        'chunk_stored' => LiveStage.store,
        _ => null,
      };

  bool get isChat => kind.startsWith('chat_');
  bool get isDiagnosis => kind.startsWith('diagnosis_');
}

enum LiveStage { sensors, backend, window, chunk, embed, store }

class LiveWindow {
  const LiveWindow({required this.start, required this.end, required this.secondsLeft, required this.readings, required this.values});

  factory LiveWindow.fromJson(Map<String, dynamic> j) => LiveWindow(
        start: time(j['start']) ?? DateTime.now(),
        end: time(j['end']) ?? DateTime.now(),
        secondsLeft: integer(j['seconds_left']),
        readings: integer(j['readings']),
        values: integer(j['values']),
      );

  final DateTime start;
  final DateTime end;
  final int secondsLeft;
  final int readings;
  final int values;
}

/// Snapshot used to draw the Live view before (and between) streamed events.
class LiveState {
  const LiveState({
    required this.window,
    required this.windowMinutes,
    required this.latest,
    required this.knowledgeByType,
    required this.counters,
    required this.pendingWindows,
    required this.embedder,
    required this.lastEventId,
    required this.events,
    required this.serverOffset,
    this.switches = const SwitchState(),
  });

  factory LiveState.fromJson(Map<String, dynamic> j) {
    final server = time(j['server_time']);
    return LiveState(
      window: LiveWindow.fromJson(map(j['current_window'])),
      windowMinutes: integer(j['window_minutes'], 15),
      latest: maps(j['latest']).map(SensorReading.fromJson).toList(),
      knowledgeByType: map(j['knowledge_by_type']).map((k, v) => MapEntry(k, integer(v))),
      counters: map(j['counters']).map((k, v) => MapEntry(k, integer(v))),
      pendingWindows: integer(j['pending_windows']),
      embedder: str(j['embedder']),
      lastEventId: integer(j['last_event_id']),
      switches: SwitchState.fromJson(map(j['sensor_switches'])),
      events: maps(j['events']).map(LiveEvent.fromJson).toList(),
      // Phone and server clocks differ; the window countdown uses the server's time.
      serverOffset: server == null ? Duration.zero : server.difference(DateTime.now()),
    );
  }

  final LiveWindow window;
  final int windowMinutes;
  final List<SensorReading> latest;
  final Map<String, int> knowledgeByType;
  final Map<String, int> counters;
  final int pendingWindows;
  final String embedder;
  final int lastEventId;
  final List<LiveEvent> events;
  final Duration serverOffset;
  final SwitchState switches;
}

/// Short label and unit for a sensor key, for compact "latest value" chips.
String liveValueText(String sensor, num value) {
  final v = value is int || value == value.roundToDouble() ? value.toStringAsFixed(0) : value.toStringAsFixed(1);
  return switch (sensor) {
    'temperature' => 'Temp $v°C',
    'humidity' => 'Humidity $v%',
    'soil_moisture' => 'Soil $v%',
    'distance_cm' => 'Obstacle $v cm',
    'battery_v' => 'Battery ${value.toStringAsFixed(2)} V',
    _ => '${sensor.toUpperCase()} $v',
  };
}
