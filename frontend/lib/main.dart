import 'package:flutter/material.dart';

import 'app/app_services.dart';
import 'app/gaia_app.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  final services = await AppServices.create();
  runApp(GaiaApp(services: services));
}
