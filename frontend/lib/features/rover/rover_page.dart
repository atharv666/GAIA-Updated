import 'dart:math' as math;

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';

import '../../app/app_services.dart';
import '../../core/constants/app_constants.dart';
import '../../core/network/api_client.dart';
import '../../core/theme/app_tokens.dart';
import '../../core/widgets/gaia_widgets.dart';
import '../../core/widgets/mjpeg_view.dart';
import '../../models/rover_models.dart';
import '../../services/rover_repository.dart';
import 'sensor_switch_panel.dart';

class RoverPage extends StatefulWidget {
  const RoverPage({super.key, this.active = true});
  final bool active;
  @override
  State<RoverPage> createState() => _RoverPageState();
}

class _RoverPageState extends State<RoverPage> {
  DriveController? _drive;
  bool joystickMode = false;
  bool capturing = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    _drive ??= DriveController(GaiaScope.of(context).rover);
  }

  @override
  void didUpdateWidget(RoverPage old) {
    super.didUpdateWidget(old);
    if (!widget.active && old.active) _drive?.release(); // never keep driving from a hidden tab
  }

  @override
  void dispose() {
    _drive?.dispose();
    super.dispose();
  }

  void _toast(String message) {
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(message), behavior: SnackBarBehavior.floating));
  }

  Future<void> _arm(bool deploy) async {
    final services = GaiaScope.of(context);
    try {
      await _drive!.release();
      final r = await services.rover.arm(deploy: deploy);
      _toast(r['ok'] == false ? '${r['error'] ?? 'Rejected'}' : (deploy ? 'Lowering the soil probe…' : 'Raising the soil probe'));
      services.monitor.refresh();
    } on ApiException catch (e) {
      _toast(e.message);
    }
  }

  Future<void> _captureFromRover() async {
    final services = GaiaScope.of(context);
    setState(() => capturing = true);
    try {
      await _drive!.release();
      final bytes = await services.rover.captureStill();
      services.sendToDiagnosis(PendingCapture(bytes, source: 'rover', filename: 'rover.jpg'), diagnoseTab: GaiaRoutes.diagnosis);
    } on ApiException catch (e) {
      _toast(e.message);
    } finally {
      if (mounted) setState(() => capturing = false);
    }
  }

  Future<void> _captureFromPhone(ImageSource source) async {
    final services = GaiaScope.of(context);
    final file = await ImagePicker().pickImage(source: source, maxWidth: 2048, imageQuality: 92);
    if (file == null) return;
    final bytes = await file.readAsBytes();
    services.sendToDiagnosis(PendingCapture(bytes, source: 'phone', filename: file.name), diagnoseTab: GaiaRoutes.diagnosis);
  }

  void _showCapture() => showModalBottomSheet<void>(
        context: context,
        showDragHandle: true,
        builder: (sheet) => Padding(
          padding: const EdgeInsets.fromLTRB(22, 6, 22, 30),
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Text('Capture for diagnosis', style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
            const SizedBox(height: 6),
            const Text('Both camera paths feed Gaia’s independent VLM pipeline.', style: TextStyle(color: GaiaColors.muted)),
            const SizedBox(height: 18),
            Row(children: [
              Expanded(
                child: FilledButton.tonalIcon(
                  onPressed: () {
                    Navigator.pop(sheet);
                    _captureFromRover();
                  },
                  icon: const Icon(Icons.camera_alt_outlined),
                  label: const Text('Rover camera'),
                ),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: FilledButton.tonalIcon(
                  onPressed: () {
                    Navigator.pop(sheet);
                    _captureFromPhone(ImageSource.camera);
                  },
                  icon: const Icon(Icons.photo_camera_front_outlined),
                  label: const Text('Phone camera'),
                ),
              ),
            ]),
          ]),
        ),
      );

  @override
  Widget build(BuildContext context) {
    final services = GaiaScope.of(context);
    final drive = _drive!;
    return SingleChildScrollView(
      child: Column(children: [
        const GaiaHeader(eyebrow: 'Rover control', title: 'See the field\nthrough Gaia.', subtitle: 'Drive, inspect, and capture from anywhere.'),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: GaiaSpacing.lg),
          child: Column(children: [
            ClipRRect(
              borderRadius: BorderRadius.circular(GaiaRadius.card),
              child: Container(
                height: 230,
                color: const Color(0xFF1B2E28),
                child: Stack(children: [
                  Positioned.fill(child: CustomPaint(painter: _GridPainter())),
                  if (widget.active)
                    Positioned.fill(
                      child: ListenableBuilder(
                        listenable: services.config,
                        builder: (context, _) => MjpegView(
                          streamUrl: services.rover.streamUrl,
                          snapshotUrl: services.rover.snapshotUrl,
                          placeholder: const _FeedMessage(title: 'CONNECTING TO CAMERA', detail: 'Live MJPEG feed'),
                          errorBuilder: (context, error, retry) => _FeedMessage(
                            title: 'CAMERA OFFLINE',
                            detail: '$error\n${services.rover.streamUrl}',
                            onRetry: retry,
                          ),
                        ),
                      ),
                    )
                  else
                    const _FeedMessage(title: 'LIVE CAMERA FEED', detail: 'Paused while this tab is hidden'),
                  const Positioned(top: 16, left: 16, child: _Tag('●  LIVE')),
                  const Positioned(top: 16, right: 16, child: _Tag('CAM 01')),
                ]),
              ),
            ),
            const SizedBox(height: 14),
            Row(children: [
              Expanded(
                child: OutlinedButton.icon(
                  onPressed: capturing ? null : _showCapture,
                  icon: capturing
                      ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2))
                      : const Icon(Icons.camera_alt_outlined),
                  label: const Text('Capture image'),
                ),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: OutlinedButton.icon(onPressed: drive.release, icon: const Icon(Icons.my_location), label: const Text('Park rover')),
              ),
            ]),
            const SizedBox(height: 10),
            Row(children: [
              Expanded(
                child: FilledButton.tonalIcon(onPressed: () => _arm(true), icon: const Icon(Icons.south, size: 18), label: const Text('Lower soil probe')),
              ),
              const SizedBox(width: 10),
              Expanded(
                child: FilledButton.tonalIcon(onPressed: () => _arm(false), icon: const Icon(Icons.north, size: 18), label: const Text('Raise probe')),
              ),
            ]),
            const SizedBox(height: 18),
            SensorSwitchPanel(active: widget.active),
            const SizedBox(height: 22),
            Row(mainAxisAlignment: MainAxisAlignment.spaceBetween, children: [
              const Text('Drive controls', style: TextStyle(fontSize: 19, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
              ValueListenableBuilder<String>(
                valueListenable: drive.state,
                builder: (_, s, __) => Text(s.toUpperCase(), style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w800, color: GaiaColors.leaf)),
              ),
            ]),
            const SizedBox(height: 12),
            Container(
              padding: const EdgeInsets.all(18),
              decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(25)),
              child: Column(children: [
                SegmentedButton<bool>(
                  segments: const [
                    ButtonSegment(value: false, icon: Icon(Icons.gamepad_outlined), label: Text('D-pad')),
                    ButtonSegment(value: true, icon: Icon(Icons.control_camera), label: Text('Joystick')),
                  ],
                  selected: {joystickMode},
                  onSelectionChanged: (v) {
                    drive.release();
                    setState(() => joystickMode = v.first);
                  },
                ),
                const SizedBox(height: 16),
                if (joystickMode) _Joystick(drive: drive) else _DPad(drive: drive),
                const SizedBox(height: 12),
                Row(children: [
                  const Text('Speed', style: TextStyle(fontSize: 12, color: GaiaColors.muted)),
                  Expanded(
                    child: StatefulBuilder(
                      builder: (context, setLocal) => Slider(
                        value: drive.speed.toDouble(),
                        min: 80,
                        max: 255,
                        divisions: 7,
                        label: '${drive.speed}',
                        onChanged: (v) => setLocal(() => drive.speed = v.round()),
                      ),
                    ),
                  ),
                ]),
                ValueListenableBuilder<String?>(
                  valueListenable: drive.lastError,
                  builder: (_, err, __) => err == null
                      ? const Text('Hold to drive. The rover stops by itself if commands stop arriving.',
                          textAlign: TextAlign.center, style: TextStyle(fontSize: 10.5, color: GaiaColors.muted))
                      : Text(err, textAlign: TextAlign.center, style: const TextStyle(fontSize: 11, color: GaiaColors.terracotta)),
                ),
              ]),
            ),
            const SizedBox(height: 18),
            ListenableBuilder(listenable: services.monitor, builder: (context, _) => _RoverStatusCard(status: services.monitor.rover, error: services.monitor.roverError)),
            const SizedBox(height: 28),
          ]),
        ),
      ]),
    );
  }
}

class _DPad extends StatelessWidget {
  const _DPad({required this.drive});
  final DriveController drive;

  Widget _button(IconData icon, String dir, {bool stop = false}) => Listener(
        onPointerDown: (_) => stop ? drive.release() : drive.hold(dir),
        onPointerUp: (_) => stop ? null : drive.release(),
        onPointerCancel: (_) => drive.release(),
        child: Container(
          width: 64,
          height: 64,
          decoration: BoxDecoration(
            color: stop ? GaiaColors.terracotta : const Color(0xFFEAF1E7),
            borderRadius: BorderRadius.circular(20),
          ),
          child: Icon(icon, size: 30, color: stop ? Colors.white : GaiaColors.leaf),
        ),
      );

  @override
  Widget build(BuildContext context) => Column(children: [
        _button(Icons.keyboard_arrow_up, 'forward'),
        const SizedBox(height: 10),
        Row(mainAxisAlignment: MainAxisAlignment.center, children: [
          _button(Icons.keyboard_arrow_left, 'left'),
          const SizedBox(width: 18),
          _button(Icons.stop_rounded, 'stop', stop: true),
          const SizedBox(width: 18),
          _button(Icons.keyboard_arrow_right, 'right'),
        ]),
        const SizedBox(height: 10),
        _button(Icons.keyboard_arrow_down, 'reverse'),
      ]);
}

/// Drag inside the circle: up/down = throttle, left/right = steering.
class _Joystick extends StatefulWidget {
  const _Joystick({required this.drive});
  final DriveController drive;
  @override
  State<_Joystick> createState() => _JoystickState();
}

class _JoystickState extends State<_Joystick> {
  static const radius = 90.0;
  Offset knob = Offset.zero;

  void _update(Offset local) {
    var v = local - const Offset(radius, radius);
    if (v.distance > radius) v = v / v.distance * radius;
    setState(() => knob = v);
    widget.drive.joystick(-v.dy / radius, v.dx / radius);
  }

  void _end() {
    setState(() => knob = Offset.zero);
    widget.drive.release();
  }

  @override
  Widget build(BuildContext context) => GestureDetector(
        onPanStart: (d) => _update(d.localPosition),
        onPanUpdate: (d) => _update(d.localPosition),
        onPanEnd: (_) => _end(),
        onPanCancel: _end,
        child: SizedBox(
          width: radius * 2,
          height: radius * 2,
          child: Stack(children: [
            Container(decoration: const BoxDecoration(color: Color(0xFFEAF1E7), shape: BoxShape.circle)),
            Center(child: Container(width: 6, height: 6, decoration: const BoxDecoration(color: GaiaColors.line, shape: BoxShape.circle))),
            Positioned(
              left: radius + knob.dx - 28,
              top: radius + knob.dy - 28,
              child: Transform.rotate(
                angle: math.atan2(knob.dy, knob.dx),
                child: Container(
                  width: 56,
                  height: 56,
                  decoration: BoxDecoration(color: GaiaColors.leaf, shape: BoxShape.circle, boxShadow: [
                    BoxShadow(color: GaiaColors.leaf.withValues(alpha: .3), blurRadius: 12, offset: const Offset(0, 4)),
                  ]),
                  child: const Icon(Icons.open_with, color: Colors.white),
                ),
              ),
            ),
          ]),
        ),
      );
}

class _RoverStatusCard extends StatelessWidget {
  const _RoverStatusCard({required this.status, required this.error});
  final RoverStatus? status;
  final String? error;

  @override
  Widget build(BuildContext context) {
    final s = status;
    final rows = s == null
        ? [('Status', error ?? 'Connecting…')]
        : [
            ('Device', '${s.deviceId} · ${s.firmware}'),
            ('Wi-Fi', '${s.wifiMode} · ${s.ssid}${s.rssi == null ? '' : ' · ${s.rssi} dBm'}'),
            ('Controller', s.nanoConnected ? 'Arduino Nano connected' : 'Nano not responding'),
            ('Soil probe', s.arm),
            ('Obstacle', s.obstacleCm == null ? 'clear' : '${s.obstacleCm} cm'),
            ('Battery', s.batteryV == null ? '—' : '${s.batteryV!.toStringAsFixed(2)} V'),
            ('Server push', s.backendPushOk == null ? '—' : (s.backendPushOk! ? 'ok · ${s.queued} queued' : 'failing · ${s.queued} queued')),
          ];
    return Container(
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(color: GaiaColors.water, borderRadius: BorderRadius.circular(20)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Icon(s == null ? Icons.wifi_off : Icons.route, color: s == null ? GaiaColors.terracotta : GaiaColors.leaf),
          const SizedBox(width: 10),
          Text(s == null ? 'Rover offline' : 'Rover online', style: const TextStyle(fontWeight: FontWeight.w800, color: GaiaColors.ink)),
        ]),
        const SizedBox(height: 10),
        for (final (k, v) in rows)
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 2),
            child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
              SizedBox(width: 92, child: Text(k, style: const TextStyle(fontSize: 11.5, color: GaiaColors.muted))),
              Expanded(child: Text(v, style: const TextStyle(fontSize: 11.5, fontWeight: FontWeight.w700, color: GaiaColors.ink))),
            ]),
          ),
      ]),
    );
  }
}

class _FeedMessage extends StatelessWidget {
  const _FeedMessage({required this.title, required this.detail, this.onRetry});
  final String title, detail;
  final VoidCallback? onRetry;
  @override
  Widget build(BuildContext context) => Center(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(mainAxisSize: MainAxisSize.min, children: [
            Container(
              padding: const EdgeInsets.all(14),
              decoration: BoxDecoration(color: Colors.white.withValues(alpha: .1), shape: BoxShape.circle),
              child: const Icon(Icons.videocam_outlined, color: GaiaColors.lime, size: 30),
            ),
            const SizedBox(height: 10),
            Text(title, style: const TextStyle(color: GaiaColors.lime, fontWeight: FontWeight.w800, letterSpacing: 1.4, fontSize: 11)),
            const SizedBox(height: 5),
            Text(detail, textAlign: TextAlign.center, style: const TextStyle(color: Colors.white70, fontSize: 11)),
            if (onRetry != null) TextButton(onPressed: onRetry, child: const Text('Retry', style: TextStyle(color: GaiaColors.lime))),
          ]),
        ),
      );
}

class _Tag extends StatelessWidget {
  final String text;
  const _Tag(this.text);
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 6),
        decoration: BoxDecoration(color: Colors.black.withValues(alpha: .3), borderRadius: BorderRadius.circular(20)),
        child: Text(text, style: const TextStyle(color: Colors.white, fontSize: 10, fontWeight: FontWeight.bold)),
      );
}

class _GridPainter extends CustomPainter {
  @override
  void paint(Canvas canvas, Size size) {
    final paint = Paint()
      ..color = Colors.white.withValues(alpha: .06)
      ..strokeWidth = 1;
    for (var x = 0.0; x < size.width; x += 28) {
      canvas.drawLine(Offset(x, 0), Offset(x, size.height), paint);
    }
    for (var y = 0.0; y < size.height; y += 28) {
      canvas.drawLine(Offset(0, y), Offset(size.width, y), paint);
    }
  }

  @override
  bool shouldRepaint(covariant CustomPainter oldDelegate) => false;
}
