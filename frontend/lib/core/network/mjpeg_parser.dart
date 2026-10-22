import 'package:flutter/foundation.dart';

/// Incremental parser for `multipart/x-mixed-replace` MJPEG streams.
///
/// Instead of relying on the multipart boundary (which differs between the ESP32 firmware and
/// other cameras), frames are cut at the JPEG start (FF D8) and end (FF D9) markers.
class MjpegFrameParser {
  MjpegFrameParser({this.maxBuffer = 3 * 1024 * 1024});

  final int maxBuffer;
  Uint8List _buffer = Uint8List(0);

  int get buffered => _buffer.length;

  /// Feeds a network chunk; returns the newest complete frame, if any.
  Uint8List? add(List<int> chunk) {
    final merged = Uint8List(_buffer.length + chunk.length)
      ..setAll(0, _buffer)
      ..setAll(_buffer.length, chunk);
    Uint8List? latest;
    var consumed = 0;
    while (true) {
      final soi = _find(merged, 0xD8, consumed);
      if (soi < 0) {
        consumed = merged.isEmpty ? 0 : merged.length - 1; // keep a trailing 0xFF
        break;
      }
      final eoi = _find(merged, 0xD9, soi + 2);
      if (eoi < 0) {
        consumed = soi;
        break;
      }
      latest = Uint8List.fromList(Uint8List.sublistView(merged, soi, eoi + 2));
      consumed = eoi + 2;
    }
    _buffer = consumed >= merged.length ? Uint8List(0) : Uint8List.fromList(Uint8List.sublistView(merged, consumed));
    if (_buffer.length > maxBuffer) _buffer = Uint8List(0); // corrupted stream guard
    return latest;
  }

  void reset() => _buffer = Uint8List(0);

  static int _find(Uint8List data, int second, int from) {
    for (var i = from; i < data.length - 1; i++) {
      if (data[i] == 0xFF && data[i + 1] == second) return i;
    }
    return -1;
  }
}
