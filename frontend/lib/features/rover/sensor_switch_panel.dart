import 'dart:async';

import 'package:flutter/material.dart';

import '../../app/app_services.dart';
import '../../core/network/api_client.dart';
import '../../core/theme/app_tokens.dart';
import '../../models/sensor_switches.dart';

/// Per-sensor power switches. Talks to the rover directly (`/api/sensors`), like driving does.
///
/// A switched-off sensor produces no readings, so the AI can explain the gap later. Gas sensors need about a
/// minute to warm up after being switched on.
class SensorSwitchPanel extends StatefulWidget {
  const SensorSwitchPanel({super.key, this.active = true});
  final bool active;
  @override
  State<SensorSwitchPanel> createState() => _SensorSwitchPanelState();
}

class _SensorSwitchPanelState extends State<SensorSwitchPanel> {
  RoverSensors? sensors;
  String? error;
  final Set<String> busy = {};
  Timer? _timer;
  bool _started = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (!_started) {
      _started = true;
      _refresh();
    }
    _schedule();
  }

  @override
  void didUpdateWidget(SensorSwitchPanel old) {
    super.didUpdateWidget(old);
    if (widget.active != old.active) {
      if (widget.active) _refresh();
      _schedule();
    }
  }

  void _schedule() {
    _timer?.cancel();
    _timer = widget.active ? Timer.periodic(const Duration(seconds: 6), (_) => _refresh()) : null;
  }

  Future<void> _refresh() async {
    try {
      final s = await GaiaScope.of(context).rover.sensors();
      if (mounted && busy.isEmpty) {
        setState(() {
          sensors = s;
          error = null;
        });
      }
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  Future<void> _set(Map<String, bool> request) async {
    final current = sensors;
    if (current == null) return;
    final rover = GaiaScope.of(context).rover;
    setState(() {
      busy.addAll(request.keys);
      sensors = current.copyWith(switches: {...current.switches, ...request}); // optimistic
    });
    try {
      final fresh = await rover.setSensors(request);
      if (mounted) setState(() => sensors = fresh);
    } on ApiException catch (e) {
      if (mounted) {
        setState(() => sensors = current);
        ScaffoldMessenger.of(context)
          ..hideCurrentSnackBar()
          ..showSnackBar(SnackBar(content: Text(e.message), behavior: SnackBarBehavior.floating));
      }
    } finally {
      if (mounted) setState(() => busy.removeAll(request.keys));
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final s = sensors;
    return Container(
      padding: const EdgeInsets.fromLTRB(16, 14, 16, 14),
      decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(25)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          const Icon(Icons.power_settings_new, size: 20, color: GaiaColors.leaf),
          const SizedBox(width: 8),
          const Expanded(child: Text('Sensors', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w800, color: GaiaColors.ink))),
          if (s != null) ...[
            TextButton(onPressed: busy.isEmpty ? () => _set({for (final t in s.toggles) t: true}) : null, child: const Text('All on')),
            TextButton(onPressed: busy.isEmpty ? () => _set({for (final t in s.toggles) t: false}) : null, child: const Text('Video only')),
          ],
        ]),
        const SizedBox(height: 4),
        if (s == null)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 10),
            child: Row(children: [
              Expanded(
                child: Text(error ?? 'Reading the sensor switches…', style: const TextStyle(fontSize: 12, color: GaiaColors.muted)),
              ),
              if (error != null) TextButton(onPressed: _refresh, child: const Text('Retry')),
            ]),
          )
        else ...[
          for (final t in s.toggles) _row(t, s),
          const SizedBox(height: 6),
          Text(
            s.anyOn
                ? 'A switched-off sensor records nothing. Gas sensors need about a minute to warm up after switching on.'
                : 'Video-only mode: every sensor is off and no readings are recorded.',
            style: const TextStyle(fontSize: 10.5, height: 1.4, color: GaiaColors.muted),
          ),
        ],
      ]),
    );
  }

  Widget _row(String toggle, RoverSensors s) {
    final on = s.switches[toggle] ?? false;
    final warming = on && s.warming.contains(toggle);
    final subtitle = !on ? 'Off · no readings' : (warming ? 'Warming up…' : 'On');
    final color = !on ? GaiaColors.muted : (warming ? const Color(0xFF7A5A12) : GaiaColors.leaf);
    return SwitchListTile.adaptive(
      key: ValueKey('switch-$toggle'),
      dense: true,
      contentPadding: EdgeInsets.zero,
      activeTrackColor: GaiaColors.leaf,
      activeThumbColor: Colors.white,
      value: on,
      onChanged: busy.contains(toggle) ? null : (v) => _set({toggle: v}),
      title: Text(toggleLong(toggle), style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: GaiaColors.ink)),
      subtitle: Text(subtitle, style: TextStyle(fontSize: 11, color: color)),
    );
  }
}
