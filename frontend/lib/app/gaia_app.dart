import 'package:flutter/material.dart';

import '../core/theme/app_theme.dart';
import '../features/shell/gaia_shell.dart';
import 'app_services.dart';

class GaiaApp extends StatelessWidget {
  const GaiaApp({super.key, required this.services});

  final AppServices services;

  @override
  Widget build(BuildContext context) => GaiaScope(
        services: services,
        child: MaterialApp(
          debugShowCheckedModeBanner: false,
          title: 'Gaia Farm Assistant',
          theme: GaiaTheme.light(),
          home: const GaiaShell(),
        ),
      );
}
