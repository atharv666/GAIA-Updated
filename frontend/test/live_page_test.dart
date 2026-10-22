import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:gaia_farm_assistant/app/app_services.dart';
import 'package:gaia_farm_assistant/features/live/live_page.dart';

/// A tiny stand-in for the backend: `/live/state` plus a `/live/stream` the test can push events into.
class FakeLiveServer {
  late final HttpServer server;
  final streams = <HttpResponse>[];

  Future<void> start() async {
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((request) async {
      final res = request.response;
      if (request.uri.path == '/api/v1/live/state') {
        final now = DateTime.now().toUtc();
        res
          ..headers.contentType = ContentType.json
          ..write(jsonEncode({
            'server_time': now.toIso8601String(),
            'window_minutes': 15,
            'current_window': {
              'start': now.subtract(const Duration(minutes: 8)).toIso8601String(),
              'end': now.add(const Duration(minutes: 7)).toIso8601String(),
              'seconds_left': 420,
              'readings': 96,
              'values': 480,
            },
            'latest': [
              {'sensor': 'temperature', 'label': 'Air temperature', 'value': 27.6, 'unit': '°C', 'display': '27.6°C', 'ts': now.toIso8601String()},
              {'sensor': 'mq5', 'label': 'MQ5', 'value': 208, 'unit': 'ADC', 'display': '208 ADC', 'ts': now.toIso8601String()},
            ],
            'sensor_switches': {
              'device_id': 'rover-01',
              'connected': true,
              'switches': [
                {'toggle': 'dht11', 'state': 'on', 'since': now.toIso8601String()},
                {'toggle': 'mq5', 'state': 'on', 'since': now.toIso8601String()},
                {'toggle': 'mq6', 'state': 'off', 'since': now.toIso8601String()},
              ],
            },
            'pending_windows': 0,
            'knowledge_by_type': {'sensor_window': 12, 'farm_note': 5},
            'embedder': 'gemini:gemini-embedding-001:768',
            'counters': {'readings': 120},
            'last_event_id': 3,
            'events': [],
          }));
        await res.close();
      } else if (request.uri.path == '/api/v1/live/stream') {
        res.headers.contentType = ContentType('text', 'event-stream');
        res.bufferOutput = false;
        res.write('event: hello\ndata: {"last_event_id": 3}\n\n');
        await res.flush();
        streams.add(res);
      } else {
        res.statusCode = 404;
        await res.close();
      }
    });
  }

  Future<void> push(Map<String, dynamic> event) async {
    for (final s in streams) {
      s.write('id: ${event['id']}\nevent: activity\ndata: ${jsonEncode(event)}\n\n');
      await s.flush();
    }
  }

  Future<void> stop() async {
    for (final s in streams) {
      await s.close().catchError((_) => null);
    }
    await server.close(force: true);
  }

  String get url => 'http://127.0.0.1:${server.port}';
}

void main() {
  setUpAll(() => HttpOverrides.global = null); // allow real loopback sockets inside the widget test

  testWidgets('Live view shows the pipeline and reacts to streamed events', (tester) async {
    HttpOverrides.global = null;
    final server = FakeLiveServer();
    await tester.runAsync(server.start);
    SharedPreferences.setMockInitialValues({'gaia.backendUrl': server.url});
    final services = await tester.runAsync(AppServices.create) as AppServices;

    await tester.pumpWidget(MaterialApp(home: GaiaScope(services: services, child: const Scaffold(body: LivePage()))));

    // Real socket I/O and the fake-async test clock only meet if we alternate real waits and pumps.
    Future<void> settle() async {
      for (var i = 0; i < 12; i++) {
        await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 40)));
        await tester.pump(const Duration(milliseconds: 20));
      }
    }

    await settle();
    await settle();
    expect(find.text('LIVE'), findsOneWidget);
    expect(find.text('1 · Rover sensors'), findsOneWidget);
    expect(find.text('6 · Saved to the knowledge store'), findsOneWidget);
    expect(find.text('Temp 27.6°C'), findsOneWidget); // seeded from /live/state
    expect(find.text('MQ5 208'), findsOneWidget);
    expect(find.text('MQ6 off'), findsOneWidget); // switched off: shown as muted chip, not as a value
    expect(find.text('windows 12'), findsOneWidget);
    expect(find.textContaining('96 readings so far'), findsOneWidget);
    expect(find.text('120 readings received'), findsOneWidget);

    // A streamed batch lights up the sensor box, updates the chips, the counter and the activity feed.
    await tester.runAsync(() => server.push({
          'id': 4,
          'ts': DateTime.now().toUtc().toIso8601String(),
          'kind': 'readings_received',
          'title': 'Rover sent 3 reading(s)',
          'data': {'readings': 3, 'latest': {'temperature': 28.4, 'humidity': 61.0}},
        }));
    await settle();
    expect(find.text('Temp 28.4°C'), findsOneWidget);
    expect(find.text('Humidity 61%'), findsOneWidget);
    expect(find.text('123 readings received'), findsOneWidget);
    expect(find.text('Rover sent 3 reading(s)'), findsOneWidget);
    expect(find.text('JUST NOW'), findsWidgets);

    // Switching the DHT11 off removes its values from the chips and shows it as off.
    await tester.runAsync(() => server.push({
          'id': 6,
          'ts': DateTime.now().toUtc().toIso8601String(),
          'kind': 'sensor_switched',
          'title': 'DHT11 switched off',
          'data': {'toggle': 'dht11', 'state': 'off', 'label': 'DHT11'},
        }));
    await settle();
    expect(find.text('DHT11 off'), findsOneWidget);
    expect(find.text('Temp 28.4°C'), findsNothing);
    expect(find.text('Humidity 61%'), findsNothing);
    expect(find.text('DHT11 switched off'), findsOneWidget); // in the activity feed

    // A chunk event shows its text in the chunk box.
    await tester.runAsync(() => server.push({
          'id': 7,
          'ts': DateTime.now().toUtc().toIso8601String(),
          'kind': 'chunk_created',
          'title': 'Wrote a 15-minute window summary in plain text',
          'data': {'chunk_title': 'Sensor window 16:00-16:15', 'preview': 'Field sensor readings from rover-01: humidity average 61%'},
        }));
    await settle();
    expect(find.text('Sensor window 16:00-16:15'), findsOneWidget);
    expect(find.textContaining('humidity average 61%'), findsOneWidget);

    await tester.pumpWidget(const SizedBox());
    await tester.runAsync(server.stop);
    services.dispose();
    await tester.pump(const Duration(seconds: 20)); // HTTP keep-alive timers
  });
}
