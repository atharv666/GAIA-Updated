import 'package:flutter/material.dart';

import '../../app/app_services.dart';
import '../../core/constants/app_constants.dart';
import '../chat/chat_page.dart';
import '../dashboard/dashboard_page.dart';
import '../diagnosis/diagnosis_page.dart';
import '../live/live_page.dart';
import '../rover/rover_page.dart';
import '../sensors/sensors_page.dart';

class GaiaShell extends StatefulWidget {
  const GaiaShell({super.key});
  @override
  State<GaiaShell> createState() => _GaiaShellState();
}

class _GaiaShellState extends State<GaiaShell> {
  int selected = GaiaRoutes.overview;
  AppServices? _services;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final services = GaiaScope.of(context);
    if (!identical(services, _services)) {
      _services?.requestedTab.removeListener(_onTabRequest);
      _services = services..requestedTab.addListener(_onTabRequest);
    }
  }

  void _onTabRequest() {
    final tab = _services?.requestedTab.value;
    if (tab != null && mounted) {
      setState(() => selected = tab);
      _services!.requestedTab.value = null;
    }
  }

  @override
  void dispose() {
    _services?.requestedTab.removeListener(_onTabRequest);
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Scaffold(
        body: SafeArea(
          child: IndexedStack(index: selected, children: [
            DashboardPage(active: selected == GaiaRoutes.overview),
            RoverPage(active: selected == GaiaRoutes.rover),
            const DiagnosisPage(),
            const ChatPage(),
            SensorsPage(active: selected == GaiaRoutes.sensors),
            LivePage(active: selected == GaiaRoutes.live),
          ]),
        ),
        bottomNavigationBar: NavigationBar(
          selectedIndex: selected,
          onDestinationSelected: (value) => setState(() => selected = value),
          backgroundColor: Colors.white,
          indicatorColor: const Color(0xFFDDEEDD),
          height: 72,
          labelBehavior: NavigationDestinationLabelBehavior.alwaysShow,
          destinations: const [
            NavigationDestination(icon: Icon(Icons.space_dashboard_outlined), selectedIcon: Icon(Icons.space_dashboard), label: 'Overview'),
            NavigationDestination(icon: Icon(Icons.toys_outlined), selectedIcon: Icon(Icons.toys), label: 'Rover'),
            NavigationDestination(icon: Icon(Icons.center_focus_strong_outlined), selectedIcon: Icon(Icons.center_focus_strong), label: 'Diagnose'),
            NavigationDestination(icon: Icon(Icons.auto_awesome_outlined), selectedIcon: Icon(Icons.auto_awesome), label: 'Gaia AI'),
            NavigationDestination(icon: Icon(Icons.speed_outlined), selectedIcon: Icon(Icons.speed), label: 'Sensors'),
            NavigationDestination(icon: Icon(Icons.stream_outlined), selectedIcon: Icon(Icons.stream), label: 'Live'),
          ],
        ),
      );
}
