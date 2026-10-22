import 'package:flutter_test/flutter_test.dart';
import 'package:gaia_farm_assistant/core/network/mjpeg_parser.dart';

List<int> jpeg(int marker) => [0xFF, 0xD8, 0x01, marker, 0x02, 0xFF, 0xD9];
List<int> part(List<int> frame) => [...'\r\n--frame\r\nContent-Type: image/jpeg\r\n\r\n'.codeUnits, ...frame];

void main() {
  test('extracts a frame split across chunks', () {
    final parser = MjpegFrameParser();
    final data = part(jpeg(0x42));
    expect(parser.add(data.sublist(0, 20)), isNull);
    expect(parser.add(data.sublist(20, data.length - 1)), isNull); // ends on the 0xFF of EOI
    final frame = parser.add(data.sublist(data.length - 1));
    expect(frame, jpeg(0x42));
    expect(parser.buffered, lessThanOrEqualTo(1));
  });

  test('returns the newest frame when several arrive in one chunk', () {
    final parser = MjpegFrameParser();
    final frame = parser.add([...part(jpeg(1)), ...part(jpeg(2)), ...part(jpeg(3))]);
    expect(frame, jpeg(3));
  });

  test('drops garbage and recovers', () {
    final parser = MjpegFrameParser(maxBuffer: 64);
    expect(parser.add(List.filled(100, 0x55)), isNull);
    expect(parser.add([0xFF, 0xD8, ...List.filled(100, 0x11)]), isNull); // overflow guard resets
    expect(parser.buffered, 0);
    expect(parser.add(part(jpeg(7))), jpeg(7));
  });
}
