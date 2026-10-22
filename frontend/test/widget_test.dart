import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:gaia_farm_assistant/app/app_services.dart';
import 'package:gaia_farm_assistant/app/gaia_app.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  testWidgets('app shell renders every tab without a backend', (tester) async {
    SharedPreferences.setMockInitialValues({});
    final services = await AppServices.create();
    await tester.pumpWidget(GaiaApp(services: services));
    await tester.pump(const Duration(milliseconds: 500));

    expect(find.text('GAIA'), findsWidgets);
    for (final tab in ['Rover', 'Diagnose', 'Gaia AI', 'Sensors', 'Live', 'Overview']) {
      await tester.tap(find.text(tab).last);
      await tester.pump(const Duration(milliseconds: 500));
    }
    // Chat is reachable and offers both modes.
    await tester.tap(find.text('Gaia AI').last);
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text('Query my farm'), findsOneWidget);
    expect(find.text('Open-ended'), findsOneWidget);

    await tester.pumpWidget(const SizedBox());
    services.dispose();
    await tester.pump(const Duration(seconds: 6)); // let in-flight fake HTTP timeouts settle
  });
}
