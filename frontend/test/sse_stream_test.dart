import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:gaia_farm_assistant/core/config/app_config.dart';
import 'package:gaia_farm_assistant/core/network/api_client.dart';
import 'package:gaia_farm_assistant/core/network/sse_parser.dart';
import 'package:gaia_farm_assistant/models/ai_models.dart';
import 'package:gaia_farm_assistant/services/farm_assistant_repository.dart';

String frame(String event, Map<String, dynamic> data) => 'event: $event\ndata: ${jsonEncode(data)}\n\n';

void main() {
  group('SseParser', () {
    test('parses events split across arbitrary chunks', () {
      final parser = SseParser();
      final text = frame('delta', {'text': 'Hel'}) + frame('delta', {'text': 'lo 🌱'});
      final events = <SseEvent>[];
      for (var i = 0; i < text.length; i += 5) {
        events.addAll(parser.add(text.substring(i, i + 5 > text.length ? text.length : i + 5)));
      }
      expect(events.map((e) => e.event), ['delta', 'delta']);
      expect(jsonDecode(events[1].data)['text'], 'Hello 🌱'.substring(3));
    });

    test('handles CRLF separators, comments and multi-line data', () {
      final events = SseParser().add(': ping\r\n\r\nevent: x\r\ndata: a\r\ndata: b\r\n\r\n');
      expect(events, hasLength(1));
      expect(events.single.event, 'x');
      expect(events.single.data, 'a\nb');
    });

    test('holds back an incomplete event', () {
      final parser = SseParser();
      expect(parser.add('event: stage\ndata: {"name":"plan'), isEmpty);
      expect(parser.add('ning"}\n\n').single.data, '{"name":"planning"}');
    });
  });

  group('FarmAssistantRepository.askStream', () {
    late AppConfig config;

    setUp(() async {
      SharedPreferences.setMockInitialValues({'gaia.backendUrl': 'http://gaia.test'});
      config = await AppConfig.load();
    });

    FarmAssistantRepository repo(MockClient client) => FarmAssistantRepository(GaiaApiClient(config, client: client));

    final finalPayload = {
      'session_id': 's1',
      'answer': 'Soil is 40% [F1].',
      'sources': [],
      'grounding': {'farm_data': true},
      'follow_ups': ['More?'],
      'plan': {},
    };

    test('emits stages, deltas and the final reply in order', () async {
      final client = MockClient.streaming((request, _) async {
        expect(request.url.path, '/api/v1/chat/stream');
        final body = [
          frame('session', {'session_id': 's1'}),
          frame('stage', {'name': 'planning'}),
          frame('stage', {'name': 'generating'}),
          frame('delta', {'text': 'Soil is '}),
          frame('delta', {'text': '40%'}),
          frame('final', finalPayload),
        ].join();
        final bytes = utf8.encode(body);
        // Deliver in awkward 13-byte pieces, like a real network.
        final pieces = [for (var i = 0; i < bytes.length; i += 13) bytes.sublist(i, i + 13 > bytes.length ? bytes.length : i + 13)];
        return http.StreamedResponse(Stream.fromIterable(pieces), 200, headers: {'content-type': 'text/event-stream'});
      });
      final events = await repo(client).askStream('How is my soil?', mode: ChatMode.farm).toList();
      expect(events.whereType<ChatStage>().map((e) => e.name), ['planning', 'generating']);
      expect(events.whereType<ChatDelta>().map((e) => e.text).join(), 'Soil is 40%');
      final done = events.last as ChatDone;
      expect(done.reply.sessionId, 's1');
      expect(done.reply.message.text, 'Soil is 40% [F1].');
      expect(done.reply.message.grounding.farmData, isTrue);
    });

    test('surfaces server error events as ApiException', () async {
      final client = MockClient.streaming((request, _) async => http.StreamedResponse(
            Stream.value(utf8.encode(frame('error', {'message': 'The AI service could not answer right now.'}))),
            200,
          ));
      expect(
        repo(client).askStream('hi', mode: ChatMode.open).toList(),
        throwsA(isA<ApiException>().having((e) => e.message, 'message', contains('could not answer'))),
      );
    });

    test('falls back to POST /chat when the server has no streaming endpoint', () async {
      final client = MockClient.streaming((request, _) async {
        if (request.url.path.endsWith('/chat/stream')) {
          return http.StreamedResponse(Stream.value(utf8.encode('{"detail":"Not Found"}')), 404);
        }
        return http.StreamedResponse(Stream.value(utf8.encode(jsonEncode(finalPayload))), 200,
            headers: {'content-type': 'application/json'});
      });
      final events = await repo(client).askStream('hi', mode: ChatMode.farm).toList();
      expect(events, hasLength(1));
      expect((events.single as ChatDone).reply.message.text, 'Soil is 40% [F1].');
    });
  });
}
