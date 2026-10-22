import 'json_utils.dart';

enum SensorKind { temperature, humidity, soilMoisture, airQuality, rover }

SensorKind sensorKindOf(String key) => switch (key) {
      'temperature' => SensorKind.temperature,
      'humidity' => SensorKind.humidity,
      'soil_moisture' => SensorKind.soilMoisture,
      'mq2' || 'mq5' || 'mq6' => SensorKind.airQuality,
      _ => SensorKind.rover,
    };

/// Latest value of one sensor, as returned by `GET /telemetry/latest`.
class SensorReading {
  const SensorReading({
    required this.sensor,
    required this.label,
    required this.value,
    required this.unit,
    required this.display,
    required this.progress,
    required this.timestamp,
    this.deviceId = '',
    this.band,
    this.bandNote,
    this.rag = true,
  });

  factory SensorReading.fromJson(Map<String, dynamic> j) => SensorReading(
        sensor: str(j['sensor']),
        label: str(j['label']),
        value: dbl(j['value']),
        unit: str(j['unit']),
        display: str(j['display']),
        progress: (dblOrNull(j['progress']) ?? 0).clamp(0.0, 1.0),
        timestamp: time(j['ts']) ?? DateTime.now(),
        deviceId: str(j['device_id']),
        band: strOrNull(j['band']),
        bandNote: strOrNull(j['band_note']),
        rag: boolean(j['rag'], true),
      );

  final String sensor;
  final String label;
  final double value;
  final String unit;
  final String display;
  final double progress;
  final DateTime timestamp;
  final String deviceId;
  final String? band;
  final String? bandNote;
  final bool rag;

  SensorKind get kind => sensorKindOf(sensor);

  bool get isAlert => const {'dry', 'saturated', 'very humid', 'very hot', 'cold', 'high', 'elevated'}.contains(band);
}

class SeriesPoint {
  const SeriesPoint(this.t, this.avg, this.min, this.max, this.count);

  factory SeriesPoint.fromJson(Map<String, dynamic> j) =>
      SeriesPoint(time(j['t']) ?? DateTime.now(), dbl(j['avg']), dbl(j['min']), dbl(j['max']), integer(j['count']));

  final DateTime t;
  final double avg;
  final double min;
  final double max;
  final int count;
}

class SensorSeries {
  const SensorSeries({required this.sensor, required this.label, required this.unit, required this.points});

  factory SensorSeries.fromJson(Map<String, dynamic> j) => SensorSeries(
        sensor: str(j['sensor']),
        label: str(j['label']),
        unit: str(j['unit']),
        points: maps(j['points']).map(SeriesPoint.fromJson).toList(),
      );

  final String sensor;
  final String label;
  final String unit;
  final List<SeriesPoint> points;
}

/// Exact statistics for a period (`GET /telemetry/summary`).
class SensorSummary {
  const SensorSummary({
    required this.sensor,
    required this.label,
    required this.unit,
    required this.avg,
    required this.min,
    required this.max,
    required this.count,
    this.band,
  });

  factory SensorSummary.fromJson(Map<String, dynamic> j) => SensorSummary(
        sensor: str(j['sensor']),
        label: str(j['label']),
        unit: str(j['unit']),
        avg: dbl(j['avg']),
        min: dbl(j['min']),
        max: dbl(j['max']),
        count: integer(j['count']),
        band: strOrNull(j['band']),
      );

  final String sensor;
  final String label;
  final String unit;
  final double avg;
  final double min;
  final double max;
  final int count;
  final String? band;
}

class FarmOverview {
  const FarmOverview({
    required this.readings,
    required this.windows,
    required this.diagnoses,
    required this.knowledgeTotal,
    required this.knowledgeByType,
    this.lastReadingAt,
  });

  factory FarmOverview.fromJson(Map<String, dynamic> j) => FarmOverview(
        readings: integer(j['readings']),
        windows: integer(j['windows']),
        diagnoses: integer(j['diagnoses']),
        knowledgeTotal: integer(map(j['knowledge'])['total']),
        knowledgeByType: map(map(j['knowledge'])['by_type']).map((k, v) => MapEntry(k, integer(v))),
        lastReadingAt: time(j['last_reading_at']),
      );

  final int readings;
  final int windows;
  final int diagnoses;
  final int knowledgeTotal;
  final Map<String, int> knowledgeByType;
  final DateTime? lastReadingAt;
}
