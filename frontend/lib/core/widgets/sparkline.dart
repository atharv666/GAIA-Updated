import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../models/sensor_reading.dart';
import '../theme/app_tokens.dart';

/// Window-average line with a min–max band; breaks the line across collection gaps.
class Sparkline extends StatelessWidget {
  const Sparkline({super.key, required this.points, this.height = 90, this.color = GaiaColors.leaf});

  final List<SeriesPoint> points;
  final double height;
  final Color color;

  @override
  Widget build(BuildContext context) => SizedBox(
        height: height,
        width: double.infinity,
        child: points.isEmpty
            ? const Center(child: Text('No data in this range yet', style: TextStyle(fontSize: 11, color: GaiaColors.muted)))
            : CustomPaint(painter: _SparklinePainter(points, color)),
      );
}

class _SparklinePainter extends CustomPainter {
  _SparklinePainter(this.points, this.color);

  final List<SeriesPoint> points;
  final Color color;

  @override
  void paint(Canvas canvas, Size size) {
    final t0 = points.first.t.millisecondsSinceEpoch.toDouble();
    final t1 = math.max(points.last.t.millisecondsSinceEpoch.toDouble(), t0 + 1);
    var lo = points.map((p) => p.min).reduce(math.min);
    var hi = points.map((p) => p.max).reduce(math.max);
    if (hi - lo < 1e-9) {
      lo -= 1;
      hi += 1;
    }
    final pad = (hi - lo) * 0.1;
    lo -= pad;
    hi += pad;
    double x(SeriesPoint p) => (p.t.millisecondsSinceEpoch - t0) / (t1 - t0) * size.width;
    double y(double v) => size.height - (v - lo) / (hi - lo) * size.height;

    // Split into segments where the rover was not collecting.
    final gaps = <double>[
      for (var i = 1; i < points.length; i++) (points[i].t.difference(points[i - 1].t).inSeconds).toDouble()
    ]..sort();
    final typical = gaps.isEmpty ? 0.0 : gaps[gaps.length ~/ 2];
    final segments = <List<SeriesPoint>>[[points.first]];
    for (var i = 1; i < points.length; i++) {
      if (typical > 0 && points[i].t.difference(points[i - 1].t).inSeconds > typical * 2.5) segments.add([]);
      segments.last.add(points[i]);
    }

    final band = Paint()..color = color.withValues(alpha: 0.13);
    final line = Paint()
      ..color = color
      ..style = PaintingStyle.stroke
      ..strokeWidth = 2
      ..strokeJoin = StrokeJoin.round;
    final dot = Paint()..color = color;
    for (final seg in segments) {
      if (seg.length == 1) {
        canvas.drawCircle(Offset(x(seg.first), y(seg.first.avg)), 2.5, dot);
        continue;
      }
      final area = Path()..moveTo(x(seg.first), y(seg.first.max));
      for (final p in seg.skip(1)) {
        area.lineTo(x(p), y(p.max));
      }
      for (final p in seg.reversed) {
        area.lineTo(x(p), y(p.min));
      }
      canvas.drawPath(area..close(), band);
      final path = Path()..moveTo(x(seg.first), y(seg.first.avg));
      for (final p in seg.skip(1)) {
        path.lineTo(x(p), y(p.avg));
      }
      canvas.drawPath(path, line);
    }
  }

  @override
  bool shouldRepaint(covariant _SparklinePainter old) => old.points != points || old.color != color;
}
