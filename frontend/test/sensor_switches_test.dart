import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'package:gaia_farm_assistant/app/app_services.dart';
import 'package:gaia_farm_assistant/features/rover/sensor_switch_panel.dart';
import 'package:gaia_farm_assistant/models/sensor_switches.dart';

/// A stand-in for the rover's `/api/sensors` that remembers what it is told.
class FakeRover {
  late final HttpServer server;
  final switches = {'dht11': true, 'soil_moisture': true, 'mq5': true, 'mq6': true};
  final posts = <Map<String, dynamic>>[];
  var warming = <String>[];

  Future<void> start() async {
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    server.listen((request) async {
      final res = request.response..headers.contentType = ContentType.json;
      if (request.uri.path == '/api/sensors' && request.method == 'POST') {
        final body = jsonDecode(await utf8.decoder.bind(request).join()) as Map<String, dynamic>;
        posts.add(body);
        final requested = Map<String, dynamic>.from(body['switches'] as Map);
        requested.forEach((k, v) {
          if (v == true && switches[k] == false && k.startsWith('mq')) warming = [...warming, k];
          switches[k] = v as bool;
        });
        res.write(jsonEncode({'ok': true, ..._state()}));
      } else if (request.uri.path == '/api/sensors') {
        res.write(jsonEncode(_state()));
      } else {
        res.statusCode = 404;
      }
      await res.close();
    });
  }

  Map<String, dynamic> _state() => {'switches': switches, 'warming': warming, 'installed': switches.keys.toList()};

  String get url => 'http://127.0.0.1:${server.port}';
}

void main() {
  setUpAll(() => HttpOverrides.global = null);

  test('maps reading keys to their switches', () {
    expect(toggleForSensor('temperature'), 'dht11');
    expect(toggleForSensor('humidity'), 'dht11');
    expect(toggleForSensor('mq5'), 'mq5');
    expect(toggleForSensor('soil_moisture'), 'soil_moisture');
    expect(toggleForSensor('distance_cm'), isNull);
    expect(toggleShort('soil_moisture'), 'Soil probe');
  });

  test('parses the backend switch state', () {
    final state = SwitchState.fromJson({
      'device_id': 'rover-01',
      'last_seen': '2026-10-17T10:31:00+00:00',
      'connected': true,
      'switches': [
        {'toggle': 'dht11', 'state': 'on', 'since': '2026-10-17T06:00:00+00:00'},
        {'toggle': 'mq5', 'state': 'off', 'since': '2026-10-17T09:00:00+00:00'},
        {'toggle': 'mq6', 'state': 'off', 'since': '2026-10-17T09:00:00+00:00'},
      ],
    });
    expect(state.connected, isTrue);
    expect(state.off.map((s) => s.toggle), ['mq5', 'mq6']);
    expect(state.offSensors, {'mq5', 'mq6'});
    expect(state.of('dht11')!.on, isTrue);
    expect(state.of('soil_moisture'), isNull);
    expect(const SwitchState().isEmpty, isTrue);
  });

  test('parses the rover sensor state and keeps display order', () {
    final s = RoverSensors.fromJson({
      'switches': {'mq6': true, 'dht11': false, 'mq5': true},
      'warming': ['mq6'],
      'installed': ['dht11', 'mq5', 'mq6'],
    });
    expect(s.toggles, ['dht11', 'mq5', 'mq6']);
    expect(s.warming, ['mq6']);
    expect(s.anyOn, isTrue);
    expect(s.copyWith(switches: {'mq5': false}).anyOn, isFalse);
  });

  testWidgets('switch panel shows the rover state and sends changes', (tester) async {
    final rover = FakeRover();
    await tester.runAsync(rover.start);
    SharedPreferences.setMockInitialValues({'gaia.roverUrl': rover.url});
    final services = await tester.runAsync(AppServices.create) as AppServices;
    await tester.pumpWidget(MaterialApp(
      home: GaiaScope(services: services, child: const Scaffold(body: SingleChildScrollView(child: SensorSwitchPanel()))),
    ));

    // Real socket I/O and the fake-async test clock only meet if we alternate real waits and pumps.
    Future<void> settle() async {
      for (var i = 0; i < 10; i++) {
        await tester.runAsync(() => Future<void>.delayed(const Duration(milliseconds: 40)));
        await tester.pump(const Duration(milliseconds: 20));
      }
    }

    await settle();
    expect(find.text('DHT11 · temperature & humidity'), findsOneWidget);
    expect(find.text('MQ5 · methane / natural gas'), findsOneWidget);
    expect(find.text('Video only'), findsOneWidget);
    expect(tester.widget<SwitchListTile>(find.byKey(const ValueKey('switch-mq5'))).value, isTrue);

    // Switching MQ5 off sends exactly that change.
    await tester.tap(find.byKey(const ValueKey('switch-mq5')));
    await settle();
    expect(rover.posts.last, {'switches': {'mq5': false}});
    expect(tester.widget<SwitchListTile>(find.byKey(const ValueKey('switch-mq5'))).value, isFalse);
    expect(find.text('Off · no readings'), findsOneWidget);

    // Switching it on again shows the heater warm-up reported by the rover.
    await tester.tap(find.byKey(const ValueKey('switch-mq5')));
    await settle();
    expect(find.text('Warming up…'), findsOneWidget);

    // Video-only mode: everything off.
    await tester.tap(find.text('Video only'));
    await settle();
    expect(rover.switches.values.every((v) => !v), isTrue);
    expect(find.textContaining('Video-only mode'), findsOneWidget);

    await tester.pumpWidget(const SizedBox());
    await tester.runAsync(() => rover.server.close(force: true));
    services.dispose();
    await tester.pump(const Duration(seconds: 20)); // HTTP keep-alive timers
  });
}
