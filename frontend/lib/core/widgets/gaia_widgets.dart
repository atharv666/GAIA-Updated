import 'package:flutter/material.dart';

import '../../app/app_services.dart';
import '../../features/settings/settings_sheet.dart';
import '../../models/ai_models.dart';
import '../../models/sensor_reading.dart';
import '../theme/app_tokens.dart';

class FadeIn extends StatelessWidget {
  final Widget child;
  final int delay;
  const FadeIn({super.key, required this.child, this.delay = 0});
  @override
  Widget build(BuildContext context) => TweenAnimationBuilder<double>(
        tween: Tween(begin: 0, end: 1),
        duration: Duration(milliseconds: 420 + delay),
        curve: Curves.easeOutCubic,
        builder: (_, value, child) =>
            Opacity(opacity: value, child: Transform.translate(offset: Offset(0, 14 * (1 - value)), child: child)),
        child: child,
      );
}

class GaiaHeader extends StatelessWidget {
  final String eyebrow;
  final String title;
  final String? subtitle;
  const GaiaHeader({super.key, required this.eyebrow, required this.title, this.subtitle});
  @override
  Widget build(BuildContext context) => Padding(
        padding: const EdgeInsets.fromLTRB(GaiaSpacing.lg, 18, GaiaSpacing.lg, 12),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Container(
              width: 34,
              height: 34,
              decoration: BoxDecoration(color: GaiaColors.forest, borderRadius: BorderRadius.circular(12)),
              child: const Icon(Icons.eco, color: GaiaColors.lime, size: 20),
            ),
            const SizedBox(width: 10),
            Text('GAIA',
                style: Theme.of(context)
                    .textTheme
                    .labelLarge
                    ?.copyWith(letterSpacing: 2.2, fontWeight: FontWeight.w800, color: GaiaColors.leaf)),
            const Spacer(),
            const _OnlinePill(),
          ]),
          const SizedBox(height: 22),
          Text(eyebrow.toUpperCase(),
              style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w800, letterSpacing: 1.7, color: GaiaColors.muted)),
          const SizedBox(height: 6),
          Text(title, style: const TextStyle(fontSize: 30, height: 1.08, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
          if (subtitle != null) ...[
            const SizedBox(height: 7),
            Text(subtitle!, style: const TextStyle(fontSize: 14, color: GaiaColors.muted)),
          ],
        ]),
      );
}

/// Live connection status; tap to open Settings.
class _OnlinePill extends StatelessWidget {
  const _OnlinePill();
  @override
  Widget build(BuildContext context) {
    final services = GaiaScope.maybeOf(context);
    if (services == null) return const SizedBox.shrink();
    return ListenableBuilder(
      listenable: services.monitor,
      builder: (context, _) {
        final m = services.monitor;
        final (label, icon, ok) = switch ((m.backendOnline, m.roverOnline)) {
          (null, _) => ('CONNECTING', Icons.sync, true),
          (true, true) => ('ROVER ONLINE', Icons.wifi, true),
          (true, false) => ('SERVER ONLINE', Icons.cloud_done_outlined, true),
          (false, true) => ('SERVER OFFLINE', Icons.cloud_off_outlined, false),
          (false, false) => ('OFFLINE', Icons.wifi_off, false),
        };
        final color = ok ? GaiaColors.leaf : GaiaColors.terracotta;
        return Row(mainAxisSize: MainAxisSize.min, children: [
          InkWell(
            borderRadius: BorderRadius.circular(30),
            onTap: () => showSettingsSheet(context),
            child: Container(
              padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
              decoration: BoxDecoration(
                color: ok ? const Color(0xFFE7F1E5) : const Color(0xFFF6E3D8),
                borderRadius: BorderRadius.circular(30),
              ),
              child: Row(children: [
                Icon(icon, size: 14, color: color),
                const SizedBox(width: 5),
                Text(label, style: TextStyle(fontSize: 10, fontWeight: FontWeight.w800, color: color)),
              ]),
            ),
          ),
          IconButton(
            tooltip: 'Settings',
            visualDensity: VisualDensity.compact,
            onPressed: () => showSettingsSheet(context),
            icon: const Icon(Icons.tune, size: 20, color: GaiaColors.muted),
          ),
        ]);
      },
    );
  }
}

class SectionTitle extends StatelessWidget {
  final String title;
  final String? caption;
  final Widget? trailing;
  const SectionTitle(this.title, {super.key, this.caption, this.trailing});
  @override
  Widget build(BuildContext context) => Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: const TextStyle(fontSize: 19, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
            if (caption != null) Text(caption!, style: const TextStyle(fontSize: 11, color: GaiaColors.muted)),
          ]),
        ),
        trailing ?? const SizedBox.shrink(),
      ]);
}

Color sensorBackground(SensorKind kind) => switch (kind) {
      SensorKind.temperature => GaiaColors.soil,
      SensorKind.humidity => GaiaColors.water,
      SensorKind.soilMoisture => GaiaColors.plant,
      SensorKind.airQuality => const Color(0xFFEAE6F1),
      SensorKind.rover => const Color(0xFFEFF3EC),
    };

IconData sensorIcon(String sensor) => switch (sensor) {
      'temperature' => Icons.thermostat,
      'humidity' => Icons.water_drop,
      'soil_moisture' => Icons.grass,
      'mq2' || 'mq5' || 'mq6' => Icons.air,
      'distance_cm' => Icons.radar,
      'battery_v' => Icons.battery_5_bar,
      _ => Icons.sensors,
    };

class SensorMetricCard extends StatelessWidget {
  final SensorReading reading;
  const SensorMetricCard({super.key, required this.reading});
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(13),
        decoration: BoxDecoration(color: sensorBackground(reading.kind), borderRadius: BorderRadius.circular(20)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Icon(sensorIcon(reading.sensor), size: 19, color: GaiaColors.leaf),
            const Spacer(),
            if (reading.isAlert) const Icon(Icons.warning_amber_rounded, size: 16, color: GaiaColors.terracotta),
          ]),
          const SizedBox(height: 13),
          FittedBox(
            fit: BoxFit.scaleDown,
            alignment: Alignment.centerLeft,
            child: Text(reading.display, style: const TextStyle(fontSize: 19, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
          ),
          const SizedBox(height: 3),
          Text(reading.label, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 10, color: GaiaColors.muted)),
        ]),
      );
}

class HealthPill extends StatelessWidget {
  const HealthPill(this.status, {super.key});
  final HealthStatus status;
  @override
  Widget build(BuildContext context) {
    final (bg, fg) = switch (status) {
      HealthStatus.healthy => (const Color(0xFFCDE5BF), GaiaColors.leaf),
      HealthStatus.unhealthy => (const Color(0xFFF3D6C6), const Color(0xFF8A3F1C)),
      HealthStatus.uncertain => (const Color(0xFFF4E7C2), const Color(0xFF7A5A12)),
    };
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
      decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(20)),
      child: Text(status.name.toUpperCase(), style: TextStyle(fontSize: 10, fontWeight: FontWeight.bold, color: fg)),
    );
  }
}

/// Friendly error block with a retry action and a shortcut to Settings.
class ErrorPanel extends StatelessWidget {
  const ErrorPanel({super.key, required this.message, this.onRetry});
  final String message;
  final VoidCallback? onRetry;
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(16),
        decoration: BoxDecoration(color: const Color(0xFFF8E6DC), borderRadius: BorderRadius.circular(18)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            const Icon(Icons.cloud_off_outlined, color: GaiaColors.terracotta, size: 20),
            const SizedBox(width: 10),
            Expanded(child: Text(message, style: const TextStyle(fontSize: 12.5, color: Color(0xFF6E3419), height: 1.4))),
          ]),
          const SizedBox(height: 10),
          Wrap(spacing: 8, children: [
            if (onRetry != null) OutlinedButton.icon(onPressed: onRetry, icon: const Icon(Icons.refresh, size: 16), label: const Text('Retry')),
            TextButton.icon(onPressed: () => showSettingsSheet(context), icon: const Icon(Icons.tune, size: 16), label: const Text('Settings')),
          ]),
        ]),
      );
}
