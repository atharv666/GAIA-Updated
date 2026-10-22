/// Small helpers for defensive JSON parsing.
String str(dynamic v, [String fallback = '']) => v == null ? fallback : v.toString();

String? strOrNull(dynamic v) => v == null || v.toString().isEmpty ? null : v.toString();

double dbl(dynamic v, [double fallback = 0]) => v is num ? v.toDouble() : double.tryParse('$v') ?? fallback;

double? dblOrNull(dynamic v) => v is num ? v.toDouble() : (v == null ? null : double.tryParse('$v'));

int integer(dynamic v, [int fallback = 0]) => v is num ? v.toInt() : int.tryParse('$v') ?? fallback;

bool boolean(dynamic v, [bool fallback = false]) => v is bool ? v : fallback;

List<String> strings(dynamic v) => v is List ? v.map((e) => e.toString()).toList() : const [];

List<Map<String, dynamic>> maps(dynamic v) =>
    v is List ? v.whereType<Map>().map((e) => Map<String, dynamic>.from(e)).toList() : const [];

Map<String, dynamic> map(dynamic v) => v is Map ? Map<String, dynamic>.from(v) : const {};

DateTime? time(dynamic v) => v == null ? null : DateTime.tryParse(v.toString())?.toLocal();
