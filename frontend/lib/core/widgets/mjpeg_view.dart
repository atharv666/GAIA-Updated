import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import '../network/mjpeg_parser.dart';

/// Shows the rover's MJPEG stream.
///
/// On Android/desktop the multipart stream is parsed directly (JPEG SOI/EOI markers).
/// Browsers cannot stream through `package:http`, so web builds poll `/snapshot` instead.
class MjpegView extends StatefulWidget {
  const MjpegView({
    super.key,
    required this.streamUrl,
    required this.snapshotUrl,
    this.fit = BoxFit.cover,
    required this.placeholder,
    required this.errorBuilder,
  });

  final String streamUrl;
  final String snapshotUrl;
  final BoxFit fit;
  final Widget placeholder;
  final Widget Function(BuildContext context, String error, VoidCallback retry) errorBuilder;

  @override
  State<MjpegView> createState() => _MjpegViewState();
}

class _MjpegViewState extends State<MjpegView> {
  http.Client? _client;
  StreamSubscription<List<int>>? _sub;
  Timer? _poll;
  Timer? _retry;
  final _parser = MjpegFrameParser();
  Uint8List? _frame;
  String? _error;

  @override
  void initState() {
    super.initState();
    _connect();
  }

  @override
  void didUpdateWidget(MjpegView old) {
    super.didUpdateWidget(old);
    if (old.streamUrl != widget.streamUrl || old.snapshotUrl != widget.snapshotUrl) _connect();
  }

  void _disconnect() {
    _sub?.cancel();
    _sub = null;
    _client?.close();
    _client = null;
    _poll?.cancel();
    _poll = null;
    _retry?.cancel();
    _retry = null;
    _parser.reset();
  }

  Future<void> _connect() async {
    _disconnect();
    if (mounted) setState(() => _error = null);
    final client = http.Client();
    _client = client;
    if (kIsWeb) {
      _poll = Timer.periodic(const Duration(milliseconds: 350), (_) => _fetchSnapshot(client));
      return;
    }
    try {
      final response = await client.send(http.Request('GET', Uri.parse(widget.streamUrl))).timeout(const Duration(seconds: 8));
      if (response.statusCode != 200) throw Exception('HTTP ${response.statusCode}');
      _sub = response.stream.listen(
        _onChunk,
        onError: (Object e) => _fail('$e'),
        onDone: () => _fail('Stream ended'),
        cancelOnError: true,
      );
    } catch (e) {
      if (identical(client, _client)) _fail('Camera stream unavailable');
    }
  }

  Future<void> _fetchSnapshot(http.Client client) async {
    try {
      final r = await client.get(Uri.parse(widget.snapshotUrl)).timeout(const Duration(seconds: 4));
      if (r.statusCode == 200 && mounted) setState(() => _frame = r.bodyBytes);
    } catch (_) {
      if (_frame == null) _fail('Camera snapshot unavailable');
    }
  }

  void _fail(String message) {
    if (!mounted) return;
    _disconnect();
    setState(() => _error = message);
    _retry = Timer(const Duration(seconds: 5), _connect);
  }

  void _onChunk(List<int> chunk) {
    final frame = _parser.add(chunk);
    if (frame != null && mounted) setState(() => _frame = frame);
  }

  @override
  void dispose() {
    _disconnect();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    if (_error != null && _frame == null) return widget.errorBuilder(context, _error!, _connect);
    if (_frame == null) return widget.placeholder;
    return Image.memory(_frame!, fit: widget.fit, gaplessPlayback: true, width: double.infinity, height: double.infinity);
  }
}
