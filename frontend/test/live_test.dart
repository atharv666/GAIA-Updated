import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:gaia_farm_assistant/core/config/app_config.dart';
import 'package:gaia_farm_assistant/core/network/api_client.dart';
import 'package:gaia_farm_assistant/models/live_models.dart';
import 'package:gaia_farm_assistant/services/live_repository.dart';

String sse(String event, Map<String, dynamic> data) => 'event: $event\ndata: ${jsonEncode(data)}\n\n';

void main() {
  late AppConfig config;

  setUp(() async {
    SharedPreferences.setMockInitialValues({'gaia.backendUrl': 'http://gaia.test'});
    config = await AppConfig.load();
  });

  test('parses the live state snapshot', () {
    final state = LiveState.fromJson({
      'server_time': DateTime.now().toUtc().toIso8601String(),
      'window_minutes': 15,
      'current_window': {'start': '2026-10-17T10:30:00+00:00', 'end': '2026-10-17T10:45:00+00:00', 'seconds_left': 420, 'readings': 8, 'values': 40},
      'latest': [
        {'sensor': 'temperature', 'label': 'Air temperature', 'value': 27.6, 'unit': '°C', 'display': '27.6°C', 'ts': '2026-10-17T10:31:00+00:00'},
      ],
      'pending_windows': 2,
      'knowledge_by_type': {'sensor_window': 12, 'farm_note': 5},
      'embedder': 'gemini:gemini-embedding-001:768',
      'counters': {'readings': 120},
      'last_event_id': 7,
      'events': [
        {'id': 7, 'ts': '2026-10-17T10:31:02+00:00', 'kind': 'chunk_stored', 'title': 'Saved', 'data': {'source_type': 'sensor_window'}},
      ],
    });
    expect(state.window.readings, 8);
    expect(state.knowledgeByType['sensor_window'], 12);
    expect(state.latest.single.sensor, 'temperature');
    expect(state.events.single.stage, LiveStage.store);
    expect(state.serverOffset.inSeconds.abs(), lessThan(5));
  });

  test('maps event kinds to pipeline stages', () {
    LiveStage? stage(String kind) => LiveEvent(id: 1, at: DateTime.now(), kind: kind, title: '', data: const {}).stage;
    expect(stage('readings_received'), LiveStage.backend);
    expect(stage('window_closed'), LiveStage.window);
    expect(stage('chunk_created'), LiveStage.chunk);
    expect(stage('chunk_embedded'), LiveStage.embed);
    expect(stage('chunk_stored'), LiveStage.store);
    expect(stage('chat_started'), isNull);
  });

  test('formats sensor chips', () {
    expect(liveValueText('temperature', 27.64), 'Temp 27.6°C');
    expect(liveValueText('humidity', 61.0), 'Humidity 61%');
    expect(liveValueText('mq5', 208), 'MQ5 208');
  });

  test('streams hello and activity events from /live/stream', () async {
    final client = MockClient.streaming((request, _) async {
      expect(request.method, 'GET');
      expect(request.url.path, '/api/v1/live/stream');
      expect(request.url.queryParameters['after'], '5');
      final body = [
        sse('hello', {'last_event_id': 5}),
        sse('activity', {'id': 6, 'ts': '2026-10-17T10:31:00+00:00', 'kind': 'readings_received', 'title': 'Rover sent 3 reading(s)', 'data': {'readings': 3}}),
        ': ping\n\n',
      ].join();
      return http.StreamedResponse(Stream.value(utf8.encode(body)), 200, headers: {'content-type': 'text/event-stream'});
    });
    final events = await LiveRepository(GaiaApiClient(config, client: client)).events(after: 5).toList();
    expect(events.map((e) => e.kind), ['hello', 'readings_received']);
    expect(events.last.id, 6);
    expect(events.last.data['readings'], 3);
  });
}
