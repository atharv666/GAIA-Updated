import 'dart:async';

import 'package:flutter/material.dart';

import '../../app/app_services.dart';
import '../../core/network/api_client.dart';
import '../../core/theme/app_tokens.dart';
import '../../core/widgets/gaia_widgets.dart';
import '../../core/widgets/sparkline.dart';
import '../../models/sensor_reading.dart';
import '../../models/sensor_switches.dart';

class SensorsPage extends StatefulWidget {
  const SensorsPage({super.key, this.active = true});
  final bool active;
  @override
  State<SensorsPage> createState() => _SensorsPageState();
}

class _SensorsPageState extends State<SensorsPage> {
  List<SensorReading> latest = const [];
  SwitchState switches = const SwitchState();
  final Map<String, SensorSeries> series = {};
  int hours = 24;
  String? error;
  bool loaded = false;
  Timer? _timer;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (!loaded) {
      loaded = true;
      _load();
    }
    _schedule();
  }

  @override
  void didUpdateWidget(SensorsPage old) {
    super.didUpdateWidget(old);
    if (widget.active != old.active) {
      if (widget.active) _load();
      _schedule();
    }
  }

  void _schedule() {
    _timer?.cancel();
    _timer = widget.active ? Timer.periodic(const Duration(seconds: 15), (_) => _loadLatest()) : null;
  }

  Future<void> _loadLatest() async {
    try {
      final repo = GaiaScope.of(context).sensors;
      final items = await repo.latest();
      var state = switches;
      try {
        state = await repo.switchState();
      } on ApiException {
        // older backend: no switch information, show readings as before
      }
      if (mounted) {
        setState(() {
          latest = items;
          switches = state;
          error = null;
        });
      }
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  Future<void> _load() async {
    await _loadLatest();
    if (!mounted) return;
    final repo = GaiaScope.of(context).sensors;
    for (final r in latest.where((r) => r.rag)) {
      try {
        final s = await repo.history(r.sensor, hours: hours);
        if (mounted) setState(() => series[r.sensor] = s);
      } on ApiException {
        // keep the previous series; the latest-value error banner already explains outages
      }
    }
  }

  Future<void> _sync() async {
    final messenger = ScaffoldMessenger.of(context);
    try {
      final r = await GaiaScope.of(context).sensors.sync();
      messenger.showSnackBar(SnackBar(content: Text('Aggregated ${r['windows_aggregated']} window(s), indexed ${r['windows_indexed']}')));
      await _load();
    } on ApiException catch (e) {
      messenger.showSnackBar(SnackBar(content: Text(e.message)));
    }
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  String _ago(DateTime t) {
    final d = DateTime.now().difference(t);
    if (d.inMinutes < 1) return 'just now';
    if (d.inMinutes < 60) return '${d.inMinutes} min ago';
    if (d.inHours < 48) return '${d.inHours} h ago';
    return '${d.inDays} d ago';
  }

  @override
  Widget build(BuildContext context) {
    final newest = latest.isEmpty ? null : latest.map((r) => r.timestamp).reduce((a, b) => a.isAfter(b) ? a : b);
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(padding: EdgeInsets.zero, children: [
        const GaiaHeader(eyebrow: 'Data acquisition layer', title: 'Your field,\nmeasured.', subtitle: 'Timestamped, aggregated and indexed for “Query my farm”.'),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: GaiaSpacing.lg),
          child: Column(children: [
            Container(
              padding: const EdgeInsets.all(18),
              decoration: BoxDecoration(color: GaiaColors.forest, borderRadius: BorderRadius.circular(GaiaRadius.card)),
              child: Row(children: [
                const Icon(Icons.sync, color: GaiaColors.lime),
                const SizedBox(width: 12),
                Expanded(
                  child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                    Text(latest.isEmpty ? 'Waiting for rover readings' : 'Sensor log is active',
                        style: const TextStyle(color: Colors.white, fontWeight: FontWeight.w800)),
                    const SizedBox(height: 4),
                    Text(newest == null ? 'No data yet' : 'Last reading • ${_ago(newest)}', style: const TextStyle(color: Color(0xFFDCEBD7), fontSize: 11)),
                  ]),
                ),
                TextButton(onPressed: _sync, child: const Text('Sync', style: TextStyle(color: GaiaColors.lime, fontWeight: FontWeight.w800))),
              ]),
            ),
            const SizedBox(height: 18),
            if (error != null) ...[ErrorPanel(message: error!, onRetry: _load), const SizedBox(height: 16)],
            if (switches.off.isNotEmpty) ...[_OffBanner(state: switches), const SizedBox(height: 14)],
            ...latest.map((r) => _ReadingCard(
                  reading: r,
                  series: series[r.sensor],
                  ago: _ago(r.timestamp),
                  off: switches.offSensors.contains(r.sensor),
                  offSince: switches.of(toggleForSensor(r.sensor) ?? '')?.since,
                )),
            const SizedBox(height: 8),
            SegmentedButton<int>(
              segments: const [ButtonSegment(value: 6, label: Text('6 h')), ButtonSegment(value: 24, label: Text('24 h')), ButtonSegment(value: 168, label: Text('7 days'))],
              selected: {hours},
              onSelectionChanged: (v) {
                setState(() => hours = v.first);
                _load();
              },
            ),
            const SizedBox(height: 16),
            const Align(alignment: Alignment.centerLeft, child: Text('Architecture note', style: TextStyle(fontSize: 17, fontWeight: FontWeight.w800, color: GaiaColors.ink))),
            const SizedBox(height: 8),
            Container(
              padding: const EdgeInsets.all(16),
              decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(19)),
              child: const Text(
                'Readings are aggregated into fixed windows (min / max / average) and written as documents '
                'for Gaia’s “Query my farm” mode; numeric questions are answered from exact statistics. '
                'Soil moisture is only logged while the rover is parked with the probe lowered. MQ gas values '
                'are uncalibrated relative indices. The diagnosis pipeline never uses sensor data.',
                style: TextStyle(fontSize: 12, height: 1.5, color: Color(0xFF64746A)),
              ),
            ),
            const SizedBox(height: 28),
          ]),
        ),
      ]),
    );
  }
}

/// Lists the sensors that are switched off: their readings are paused, not missing by accident.
class _OffBanner extends StatelessWidget {
  const _OffBanner({required this.state});
  final SwitchState state;
  @override
  Widget build(BuildContext context) => Container(
        width: double.infinity,
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(color: const Color(0xFFF4E7C2), borderRadius: BorderRadius.circular(18)),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Icon(Icons.power_settings_new, size: 18, color: Color(0xFF7A5A12)),
          const SizedBox(width: 10),
          Expanded(
            child: Text(
              'Switched off: ${state.off.map((s) => toggleShort(s.toggle)).join(', ')}. '
              'These record nothing until you switch them on in the Rover tab.',
              style: const TextStyle(fontSize: 12, height: 1.4, color: Color(0xFF5E4710)),
            ),
          ),
        ]),
      );
}

class _ReadingCard extends StatelessWidget {
  final SensorReading reading;
  final SensorSeries? series;
  final String ago;
  final bool off;
  final DateTime? offSince;
  const _ReadingCard({required this.reading, required this.series, required this.ago, this.off = false, this.offSince});

  String get _since => offSince == null ? '' : ' since ${offSince!.hour.toString().padLeft(2, '0')}:${offSince!.minute.toString().padLeft(2, '0')}';

  @override
  Widget build(BuildContext context) => Container(
        margin: const EdgeInsets.only(bottom: 11),
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(color: off ? const Color(0xFFEFEFEA) : sensorBackground(reading.kind), borderRadius: BorderRadius.circular(20)),
        child: Column(children: [
          Row(children: [
            Icon(sensorIcon(reading.sensor), color: GaiaColors.leaf, size: 24),
            const SizedBox(width: 13),
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(off ? 'SWITCHED OFF$_since' : (reading.band == null ? reading.sensor.toUpperCase() : reading.band!.toUpperCase()),
                    style: TextStyle(fontSize: 10, fontWeight: FontWeight.bold, color: off ? const Color(0xFF7A5A12) : (reading.isAlert ? GaiaColors.terracotta : GaiaColors.muted), letterSpacing: 1)),
                const SizedBox(height: 3),
                Text(reading.label, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontWeight: FontWeight.w800, color: GaiaColors.ink)),
                const SizedBox(height: 3),
                Text(off ? 'Last reading $ago' : '$ago · ${reading.deviceId}', style: const TextStyle(fontSize: 10.5, color: GaiaColors.muted)),
              ]),
            ),
            const SizedBox(width: 15),
            Text(reading.display, style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, color: off ? GaiaColors.muted : GaiaColors.ink)),
          ]),
          if (reading.rag) ...[
            const SizedBox(height: 12),
            Sparkline(points: series?.points ?? const [], height: 70),
          ] else ...[
            const SizedBox(height: 10),
            ClipRRect(
              borderRadius: BorderRadius.circular(5),
              child: LinearProgressIndicator(value: reading.progress, minHeight: 5, backgroundColor: Colors.white70, color: GaiaColors.leaf),
            ),
          ],
        ]),
      );
}
