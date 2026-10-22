/// One Server-Sent Event (`event:` name plus the joined `data:` lines).
class SseEvent {
  const SseEvent(this.event, this.data);

  final String event;
  final String data;
}

/// Incremental parser for `text/event-stream` bodies.
///
/// Feed decoded text in arbitrary pieces (network chunks never align with event boundaries); complete
/// events are returned as soon as their terminating blank line has arrived. Comment lines (`:`) are ignored.
class SseParser {
  String _buffer = '';

  List<SseEvent> add(String chunk) {
    _buffer += chunk;
    final events = <SseEvent>[];
    while (true) {
      final match = RegExp(r'\r\n\r\n|\n\n|\r\r').firstMatch(_buffer);
      if (match == null) break;
      final block = _buffer.substring(0, match.start);
      _buffer = _buffer.substring(match.end);
      final event = _parseBlock(block);
      if (event != null) events.add(event);
    }
    return events;
  }

  static SseEvent? _parseBlock(String block) {
    var name = 'message';
    final data = <String>[];
    for (final line in block.split(RegExp(r'\r\n|\n|\r'))) {
      if (line.isEmpty || line.startsWith(':')) continue;
      final colon = line.indexOf(':');
      final field = colon == -1 ? line : line.substring(0, colon);
      var value = colon == -1 ? '' : line.substring(colon + 1);
      if (value.startsWith(' ')) value = value.substring(1);
      if (field == 'event') {
        name = value;
      } else if (field == 'data') {
        data.add(value);
      }
    }
    return data.isEmpty ? null : SseEvent(name, data.join('\n'));
  }
}
