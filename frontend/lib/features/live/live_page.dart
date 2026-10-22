import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../../app/app_services.dart';
import '../../core/network/api_client.dart';
import '../../core/theme/app_tokens.dart';
import '../../core/widgets/gaia_widgets.dart';
import '../../models/live_models.dart';
import '../../models/sensor_switches.dart';

/// Live view: what the system is doing right now, drawn as an animated pipeline.
///
/// Sensor readings arrive, a 15-minute window fills up, closes, is written as plain text, embedded and stored.
/// Questions and photo diagnoses show their stages in a second lane. Everything comes from `/live/stream`.
class LivePage extends StatefulWidget {
  const LivePage({super.key, this.active = true});
  final bool active;
  @override
  State<LivePage> createState() => _LivePageState();
}

class _LivePageState extends State<LivePage> with SingleTickerProviderStateMixin {
  static const _lit = Duration(milliseconds: 2600);
  static const _chatSteps = {
    'syncing': 'Syncing sensor data',
    'planning': 'Understanding the question',
    'retrieving': 'Searching your farm data',
    'searching': 'Searching the web',
    'generating': 'Writing the answer',
  };
  static const _diagnosisSteps = {'vlm': 'Looking at the photo', 'searching': 'Searching the web', 'writing': 'Saving findings'};

  late final AnimationController _pulse = AnimationController(vsync: this, duration: const Duration(milliseconds: 1800));
  final List<LiveEvent> events = [];
  final Map<LiveStage, DateTime> hits = {};
  final Map<String, double> latest = {};
  final Map<String, bool> switchOn = {}; // sensor switch -> on, from the backend and switch events
  LiveState? state;
  String? error;
  bool connected = false;
  int lastId = 0;
  int readingsTotal = 0;
  Map<String, int> store = {};
  LiveEvent? lastChunk;
  LiveEvent? lastEmbed;
  LiveEvent? lastWindow;
  final List<String> chatSeen = [];
  bool chatBusy = false;
  String chatNote = '';
  final List<String> diagSeen = [];
  bool diagBusy = false;
  String diagNote = '';

  int _generation = 0;
  Timer? _poll;
  StreamSubscription<LiveEvent>? _sub;
  Completer<void>? _closed;
  bool _started = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (widget.active && !_started) _start();
  }

  @override
  void didUpdateWidget(LivePage old) {
    super.didUpdateWidget(old);
    if (widget.active == old.active) return;
    widget.active ? _start() : _stop();
  }

  void _start() {
    _started = true;
    _pulse.repeat();
    final gen = ++_generation;
    _loadState();
    _poll?.cancel();
    _poll = Timer.periodic(const Duration(seconds: 5), (_) => _loadState());
    _run(gen);
  }

  void _stop() {
    _started = false;
    _generation++;
    _poll?.cancel();
    _sub?.cancel();
    if (_closed != null && !_closed!.isCompleted) _closed!.complete();
    _pulse.stop();
    if (mounted) setState(() => connected = false);
  }

  bool _alive(int gen) => mounted && _started && gen == _generation;

  Future<void> _loadState() async {
    try {
      final s = await GaiaScope.of(context).live.state();
      if (!mounted) return;
      setState(() {
        state = s;
        error = null;
        store = Map.of(s.knowledgeByType);
        for (final sw in s.switches.switches) {
          switchOn[sw.toggle] = sw.on;
        }
        readingsTotal = math.max(readingsTotal, s.counters['readings'] ?? 0);
        if (events.isEmpty) {
          for (final e in s.events.reversed) {
            _absorb(e);
          }
        }
        for (final r in s.latest) {
          latest.putIfAbsent(r.sensor, () => r.value);
        }
        lastId = math.max(lastId, s.lastEventId);
      });
    } on ApiException catch (e) {
      if (mounted) setState(() => error = e.message);
    }
  }

  Future<void> _run(int gen) async {
    final live = GaiaScope.of(context).live;
    while (_alive(gen)) {
      final closed = _closed = Completer<void>();
      _sub = live.events(after: lastId > 0 ? lastId : null).listen(
        (e) {
          if (_alive(gen)) _onEvent(e);
        },
        onError: (Object _) {
          if (!closed.isCompleted) closed.complete();
        },
        onDone: () {
          if (!closed.isCompleted) closed.complete();
        },
        cancelOnError: true,
      );
      await closed.future;
      await _sub?.cancel();
      if (!_alive(gen)) return;
      setState(() => connected = false);
      await Future<void>.delayed(const Duration(seconds: 3));
    }
  }

  void _onEvent(LiveEvent e) {
    setState(() {
      connected = true;
      if (e.kind == 'hello' || e.id <= lastId) return;
      lastId = e.id;
      _absorb(e, live: true);
    });
    if (e.kind == 'chunk_stored' || e.kind == 'window_closed') _loadState();
  }

  /// Folds one event into the screen state. [live] events also light up their pipeline box.
  void _absorb(LiveEvent e, {bool live = false}) {
    events.insert(0, e);
    if (events.length > 40) events.removeLast();
    final now = DateTime.now();
    if (live) {
      final st = e.stage;
      if (st != null) hits[st] = now;
    }
    switch (e.kind) {
      case 'readings_received':
        if (live) {
          hits[LiveStage.sensors] = now;
          readingsTotal += (e.data['readings'] as num?)?.toInt() ?? 0;
        }
        (e.data['latest'] as Map?)?.forEach((k, v) {
          if (v is num) latest[k.toString()] = v.toDouble();
        });
      case 'sensor_switched':
        switchOn[e.data['toggle']?.toString() ?? ''] = e.data['state'] == 'on';
      case 'window_closed':
        lastWindow = e;
      case 'chunk_created':
        lastChunk = e;
      case 'chunk_embedded':
        lastEmbed = e;
      case 'chat_started':
        chatSeen.clear();
        chatBusy = true;
        chatNote = e.title;
      case 'chat_stage':
        final s = e.data['stage']?.toString() ?? '';
        if (!chatSeen.contains(s)) chatSeen.add(s);
      case 'chat_finished':
        chatBusy = false;
        chatNote = e.title;
      case 'diagnosis_stage':
        final s = e.data['stage']?.toString() ?? '';
        if (!diagBusy) diagSeen.clear();
        diagBusy = true;
        if (!diagSeen.contains(s)) diagSeen.add(s);
        diagNote = 'Analysing a crop photo';
      case 'diagnosis_finished':
        diagBusy = false;
        diagNote = e.title;
    }
  }

  @override
  void dispose() {
    _generation++;
    _started = false;
    _poll?.cancel();
    _sub?.cancel();
    if (_closed != null && !_closed!.isCompleted) _closed!.complete();
    _pulse.dispose();
    super.dispose();
  }

  bool _isLit(LiveStage s) {
    final t = hits[s];
    return t != null && DateTime.now().difference(t) < _lit;
  }

  @override
  Widget build(BuildContext context) => ListView(
        padding: const EdgeInsets.only(bottom: 24),
        children: [
          const GaiaHeader(
            eyebrow: 'Live view',
            title: 'Watch the system\nthink.',
            subtitle: 'Sensor data in, searchable knowledge out. This is what happens, as it happens.',
          ),
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: GaiaSpacing.lg),
            child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
              _statusRow(),
              const SizedBox(height: 14),
              if (state == null && error != null)
                ErrorPanel(message: error!, onRetry: _loadState)
              else ...[
                AnimatedBuilder(animation: _pulse, builder: (_, __) => _pipeline()),
                const SizedBox(height: 22),
                const SectionTitle('Questions and photos', caption: 'The second lane: what the AI does when you ask or diagnose'),
                const SizedBox(height: 12),
                _aiLane(),
                const SizedBox(height: 22),
                const SectionTitle('Activity', caption: 'Newest first'),
                const SizedBox(height: 10),
                if (events.isEmpty)
                  const _Hint('Nothing yet. Once the rover sends data, or you ask a question, it appears here.')
                else
                  ...events.take(25).map((e) => _FeedRow(event: e, onTap: () => _openEvent(e))),
              ],
            ]),
          ),
        ],
      );

  Widget _statusRow() {
    final pending = state?.pendingWindows ?? 0;
    final color = connected ? GaiaColors.leaf : GaiaColors.terracotta;
    return Row(children: [
      AnimatedBuilder(
        animation: _pulse,
        builder: (_, __) => Container(
          width: 10,
          height: 10,
          decoration: BoxDecoration(
            color: color,
            shape: BoxShape.circle,
            boxShadow: connected ? [BoxShadow(color: color.withValues(alpha: 0.45 * (1 - _pulse.value)), blurRadius: 10, spreadRadius: 8 * _pulse.value)] : null,
          ),
        ),
      ),
      const SizedBox(width: 12),
      Text(connected ? 'LIVE' : 'RECONNECTING…', style: TextStyle(fontSize: 11, fontWeight: FontWeight.w800, letterSpacing: 1.4, color: color)),
      const Spacer(),
      if (pending > 0) Text('$pending window${pending == 1 ? '' : 's'} waiting to be saved', style: const TextStyle(fontSize: 11, color: GaiaColors.muted)),
    ]);
  }

  // ------------------------------------------------------------------ sensor pipeline

  Widget _pipeline() {
    final s = state;
    final w = s?.window;
    final total = Duration(minutes: s?.windowMinutes ?? 15);
    Duration remaining = Duration.zero;
    if (w != null && s != null) remaining = w.end.difference(DateTime.now().add(s.serverOffset));
    if (remaining.isNegative) remaining = Duration.zero;
    final progress = w == null ? 0.0 : (1 - remaining.inMilliseconds / total.inMilliseconds).clamp(0.0, 1.0);
    final mm = remaining.inMinutes.toString().padLeft(2, '0');
    final ss = (remaining.inSeconds % 60).toString().padLeft(2, '0');
    final chunk = lastChunk;
    final embedModel = lastEmbed?.data['model']?.toString() ?? s?.embedder ?? '';
    final dims = (lastEmbed?.data['dims'] as num?)?.toInt() ?? 768;

    final steps = <_StepData>[
      _StepData(
        stage: LiveStage.sensors,
        icon: Icons.sensors,
        title: '1 · Rover sensors',
        caption: 'Temperature, humidity, soil moisture and gas sensors are read every few seconds.',
        body: latest.isEmpty && switchOn.isEmpty
            ? const _Hint('Waiting for the first reading…')
            : Wrap(spacing: 6, runSpacing: 6, children: [
                for (final e in latest.entries)
                  if (switchOn[toggleForSensor(e.key)] != false) _Chip(liveValueText(e.key, e.value)),
                for (final t in kToggleOrder)
                  if (switchOn[t] == false) _Chip('${toggleShort(t)} off', muted: true),
              ]),
      ),
      _StepData(
        stage: LiveStage.backend,
        icon: Icons.cloud_download_outlined,
        title: '2 · Server receives',
        caption: 'The rover sends a batch over Wi-Fi every 15 seconds. Each value is checked and stored with its time.',
        body: Text('$readingsTotal readings received', style: const TextStyle(fontSize: 13, fontWeight: FontWeight.w700, color: GaiaColors.ink)),
      ),
      _StepData(
        stage: LiveStage.window,
        icon: Icons.hourglass_bottom,
        title: '3 · Collecting a ${s?.windowMinutes ?? 15}-minute window',
        caption: 'Readings are grouped into windows. When one closes, the minimum, maximum and average are calculated by code.',
        body: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          ClipRRect(
            borderRadius: BorderRadius.circular(8),
            child: LinearProgressIndicator(value: progress, minHeight: 9, backgroundColor: GaiaColors.line, color: GaiaColors.leaf),
          ),
          const SizedBox(height: 6),
          Text(
            w == null
                ? '—'
                : '${_hm(w.start)}–${_hm(w.end)} · ${w.readings} reading${w.readings == 1 ? '' : 's'} so far · '
                    '${remaining == Duration.zero ? 'closing now' : 'closes in $mm:$ss'}',
            style: const TextStyle(fontSize: 12, color: GaiaColors.ink),
          ),
        ]),
      ),
      _StepData(
        stage: LiveStage.chunk,
        icon: Icons.article_outlined,
        title: '4 · Written as a text chunk',
        caption: 'The window becomes a short plain-language summary. This text, not the raw numbers, is what gets searched.',
        body: chunk == null
            ? const _Hint('The first chunk appears when a window closes.')
            : InkWell(
                onTap: () => _openEvent(chunk),
                borderRadius: BorderRadius.circular(12),
                child: Container(
                  width: double.infinity,
                  padding: const EdgeInsets.all(10),
                  decoration: BoxDecoration(color: const Color(0xFFF1F5EC), borderRadius: BorderRadius.circular(12)),
                  child: AnimatedSwitcher(
                    duration: GaiaMotion.standard,
                    child: Column(
                      key: ValueKey(chunk.id),
                      crossAxisAlignment: CrossAxisAlignment.start,
                      children: [
                        Text(chunk.data['chunk_title']?.toString() ?? '', maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w800, color: GaiaColors.leaf)),
                        const SizedBox(height: 4),
                        Text(chunk.data['preview']?.toString() ?? '', maxLines: 4, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 11.5, height: 1.35, color: GaiaColors.ink)),
                      ],
                    ),
                  ),
                ),
              ),
      ),
      _StepData(
        stage: LiveStage.embed,
        icon: Icons.scatter_plot_outlined,
        title: '5 · Turned into numbers',
        caption: 'An embedding model converts the text into $dims numbers, so similar meanings can be found later.',
        body: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          _EmbeddingBars(phase: _pulse.value, active: _isLit(LiveStage.embed)),
          const SizedBox(height: 6),
          Text(embedModel.isEmpty ? '—' : embedModel, style: const TextStyle(fontSize: 11, color: GaiaColors.muted)),
        ]),
      ),
      _StepData(
        stage: LiveStage.store,
        icon: Icons.storage_rounded,
        title: '6 · Saved to the knowledge store',
        caption: 'Text and numbers are stored together (PostgreSQL + pgvector). Chat searches here.',
        body: store.isEmpty
            ? const _Hint('Nothing stored yet.')
            : Wrap(spacing: 6, runSpacing: 6, children: [
                for (final e in store.entries) _Chip('${_typeLabel(e.key)} ${e.value}'),
              ]),
      ),
    ];

    return Column(children: [
      for (var i = 0; i < steps.length; i++) ...[
        _StepTile(data: steps[i], lit: _isLit(steps[i].stage)),
        if (i < steps.length - 1) _Connector(flowing: _isLit(steps[i].stage), phase: _pulse.value),
      ],
    ]);
  }

  // ------------------------------------------------------------------ AI lane

  Widget _aiLane() {
    if (!chatBusy && chatSeen.isEmpty && !diagBusy && diagSeen.isEmpty) {
      return const _Hint('Ask Gaia a question or diagnose a photo, then come back here to watch it work.');
    }
    return Column(children: [
      if (chatSeen.isNotEmpty || chatBusy)
        _LaneCard(
          icon: Icons.auto_awesome,
          title: chatNote.isEmpty ? 'Chat' : chatNote,
          steps: _chatSteps,
          seen: chatSeen,
          busy: chatBusy,
          phase: _pulse.value,
          doneText: 'Answer delivered',
        ),
      if (diagSeen.isNotEmpty || diagBusy) ...[
        const SizedBox(height: 10),
        _LaneCard(
          icon: Icons.center_focus_strong,
          title: diagNote.isEmpty ? 'Diagnosis' : diagNote,
          steps: _diagnosisSteps,
          seen: diagSeen,
          busy: diagBusy,
          phase: _pulse.value,
          doneText: 'Diagnosis saved',
        ),
      ],
    ]);
  }

  // ------------------------------------------------------------------ details

  void _openEvent(LiveEvent e) {
    final preview = e.data['preview']?.toString();
    showModalBottomSheet<void>(
      context: context,
      showDragHandle: true,
      isScrollControlled: true,
      builder: (_) => Padding(
        padding: const EdgeInsets.fromLTRB(22, 0, 22, 28),
        child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
          Text(e.kind.replaceAll('_', ' ').toUpperCase(), style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w800, color: GaiaColors.leaf)),
          const SizedBox(height: 6),
          Text(e.title, style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
          const SizedBox(height: 4),
          Text(_hms(e.at), style: const TextStyle(fontSize: 12, color: GaiaColors.muted)),
          if (preview != null) ...[
            const SizedBox(height: 12),
            const Text('Text that is embedded and searched:', style: TextStyle(fontSize: 11, color: GaiaColors.muted)),
            const SizedBox(height: 6),
            ConstrainedBox(
              constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * 0.5),
              child: SingleChildScrollView(child: SelectableText(preview, style: const TextStyle(fontSize: 13, height: 1.45, color: GaiaColors.ink))),
            ),
          ] else if (e.data.isNotEmpty) ...[
            const SizedBox(height: 12),
            Text(e.data.entries.map((x) => '${x.key}: ${x.value}').join('\n'), style: const TextStyle(fontSize: 12, height: 1.5, color: GaiaColors.ink)),
          ],
        ]),
      ),
    );
  }
}

String _hm(DateTime t) => '${t.hour.toString().padLeft(2, '0')}:${t.minute.toString().padLeft(2, '0')}';
String _hms(DateTime t) => '${_hm(t)}:${t.second.toString().padLeft(2, '0')}';

String _typeLabel(String t) => switch (t) {
      'sensor_window' => 'windows',
      'sensor_daily' => 'daily',
      'sensor_sessions' => 'on/off logs',
      'sensor_digest_llm' => 'AI daily',
      'diagnosis' => 'diagnoses',
      'agent_finding' => 'findings',
      'farm_note' => 'notes',
      'document' => 'documents',
      _ => t,
    };

class _StepData {
  const _StepData({required this.stage, required this.icon, required this.title, required this.caption, required this.body});
  final LiveStage stage;
  final IconData icon;
  final String title;
  final String caption;
  final Widget body;
}

class _StepTile extends StatelessWidget {
  const _StepTile({required this.data, required this.lit});
  final _StepData data;
  final bool lit;

  @override
  Widget build(BuildContext context) => AnimatedContainer(
        duration: GaiaMotion.standard,
        curve: Curves.easeOut,
        width: double.infinity,
        padding: const EdgeInsets.all(14),
        decoration: BoxDecoration(
          color: lit ? const Color(0xFFEAF5DF) : Colors.white,
          borderRadius: BorderRadius.circular(GaiaRadius.card - 4),
          border: Border.all(color: lit ? GaiaColors.leaf : GaiaColors.line, width: lit ? 1.6 : 1),
          boxShadow: lit ? [BoxShadow(color: GaiaColors.leaf.withValues(alpha: 0.22), blurRadius: 16)] : null,
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            AnimatedContainer(
              duration: GaiaMotion.standard,
              width: 34,
              height: 34,
              decoration: BoxDecoration(color: lit ? GaiaColors.leaf : GaiaColors.plant, borderRadius: BorderRadius.circular(11)),
              child: Icon(data.icon, size: 19, color: lit ? Colors.white : GaiaColors.leaf),
            ),
            const SizedBox(width: 10),
            Expanded(child: Text(data.title, style: const TextStyle(fontSize: 14.5, fontWeight: FontWeight.w800, color: GaiaColors.ink))),
            if (lit) const Text('JUST NOW', style: TextStyle(fontSize: 9.5, fontWeight: FontWeight.w800, letterSpacing: 1, color: GaiaColors.leaf)),
          ]),
          const SizedBox(height: 8),
          Text(data.caption, style: const TextStyle(fontSize: 11.5, height: 1.35, color: GaiaColors.muted)),
          const SizedBox(height: 10),
          data.body,
        ]),
      );
}

/// Vertical link between two boxes; dots travel down it while data is flowing.
class _Connector extends StatelessWidget {
  const _Connector({required this.flowing, required this.phase});
  final bool flowing;
  final double phase;
  @override
  Widget build(BuildContext context) => SizedBox(
        height: 30,
        width: double.infinity,
        child: CustomPaint(painter: _ConnectorPainter(flowing: flowing, phase: phase)),
      );
}

class _ConnectorPainter extends CustomPainter {
  _ConnectorPainter({required this.flowing, required this.phase});
  final bool flowing;
  final double phase;

  @override
  void paint(Canvas canvas, Size size) {
    const x = 31.0; // lines up with the icon column
    final line = Paint()
      ..color = flowing ? GaiaColors.leaf.withValues(alpha: 0.5) : GaiaColors.line
      ..strokeWidth = 2.5;
    canvas.drawLine(const Offset(x, 0), Offset(x, size.height), line);
    if (!flowing) return;
    for (var i = 0; i < 3; i++) {
      final t = (phase * 1.0 + i / 3) % 1.0;
      canvas.drawCircle(Offset(x, size.height * t), 4.2, Paint()..color = GaiaColors.leaf.withValues(alpha: 1 - 0.55 * t));
    }
  }

  @override
  bool shouldRepaint(_ConnectorPainter old) => old.phase != phase || old.flowing != flowing;
}

class _EmbeddingBars extends StatelessWidget {
  const _EmbeddingBars({required this.phase, required this.active});
  final double phase;
  final bool active;
  @override
  Widget build(BuildContext context) => SizedBox(
        height: 28,
        width: double.infinity,
        child: CustomPaint(painter: _BarsPainter(phase: phase, active: active)),
      );
}

class _BarsPainter extends CustomPainter {
  _BarsPainter({required this.phase, required this.active});
  final double phase;
  final bool active;

  @override
  void paint(Canvas canvas, Size size) {
    const n = 32;
    const gap = 3.0;
    final w = (size.width - gap * (n - 1)) / n;
    for (var i = 0; i < n; i++) {
      final wave = active ? 0.5 + 0.5 * math.sin(phase * 2 * math.pi + i * 0.75) : 0.18 + 0.08 * math.sin(i * 1.3);
      final h = math.max(3.0, size.height * wave);
      final rect = RRect.fromRectAndRadius(Rect.fromLTWH(i * (w + gap), size.height - h, w, h), const Radius.circular(2));
      canvas.drawRRect(rect, Paint()..color = active ? GaiaColors.leaf : GaiaColors.line);
    }
  }

  @override
  bool shouldRepaint(_BarsPainter old) => old.phase != phase || old.active != active;
}

class _LaneCard extends StatelessWidget {
  const _LaneCard({
    required this.icon,
    required this.title,
    required this.steps,
    required this.seen,
    required this.busy,
    required this.phase,
    required this.doneText,
  });
  final IconData icon;
  final String title;
  final Map<String, String> steps;
  final List<String> seen;
  final bool busy;
  final double phase;
  final String doneText;

  @override
  Widget build(BuildContext context) {
    final current = seen.isEmpty ? null : seen.last;
    return Container(
      width: double.infinity,
      padding: const EdgeInsets.all(14),
      decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(20), border: Border.all(color: busy ? GaiaColors.leaf : GaiaColors.line)),
      child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
        Row(children: [
          Icon(icon, size: 18, color: GaiaColors.leaf),
          const SizedBox(width: 8),
          Expanded(child: Text(title, maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 13.5, fontWeight: FontWeight.w800, color: GaiaColors.ink))),
          if (!busy) Text(doneText, style: const TextStyle(fontSize: 10.5, fontWeight: FontWeight.w700, color: GaiaColors.leaf)),
        ]),
        const SizedBox(height: 10),
        Wrap(spacing: 6, runSpacing: 6, children: [
          for (final e in steps.entries)
            if (seen.contains(e.key) || (busy && e.key == current))
              _stepChip(e.value, active: busy && e.key == current),
        ]),
      ]),
    );
  }

  Widget _stepChip(String label, {required bool active}) {
    final glow = active ? 0.5 + 0.5 * math.sin(phase * 2 * math.pi) : 0.0;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: active ? Color.lerp(const Color(0xFFD6E8CD), GaiaColors.lime, glow) : const Color(0xFFE7F1E5),
        borderRadius: BorderRadius.circular(14),
      ),
      child: Row(mainAxisSize: MainAxisSize.min, children: [
        Icon(active ? Icons.more_horiz : Icons.check, size: 13, color: GaiaColors.leaf),
        const SizedBox(width: 5),
        Text(label, style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: GaiaColors.leaf)),
      ]),
    );
  }
}

class _FeedRow extends StatelessWidget {
  const _FeedRow({required this.event, required this.onTap});
  final LiveEvent event;
  final VoidCallback onTap;

  @override
  Widget build(BuildContext context) {
    final (icon, color) = switch (event.kind) {
      'readings_received' => (Icons.sensors, GaiaColors.leaf),
      'sensor_switched' => (Icons.power_settings_new, const Color(0xFF7A5A12)),
      'window_closed' => (Icons.hourglass_bottom, const Color(0xFF7A5A12)),
      'chunk_created' => (Icons.article_outlined, GaiaColors.leaf),
      'chunk_embedded' => (Icons.scatter_plot_outlined, const Color(0xFF5B4A8A)),
      'chunk_stored' => (Icons.storage_rounded, GaiaColors.forest),
      _ when event.isChat => (Icons.auto_awesome, GaiaColors.terracotta),
      _ when event.isDiagnosis => (Icons.center_focus_strong, GaiaColors.terracotta),
      _ => (Icons.circle, GaiaColors.muted),
    };
    return FadeIn(
      child: InkWell(
        onTap: onTap,
        borderRadius: BorderRadius.circular(12),
        child: Padding(
          padding: const EdgeInsets.symmetric(vertical: 7),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Icon(icon, size: 16, color: color),
            const SizedBox(width: 10),
            Expanded(child: Text(event.title, style: const TextStyle(fontSize: 12.5, height: 1.3, color: GaiaColors.ink))),
            const SizedBox(width: 8),
            Text(_hms(event.at), style: const TextStyle(fontSize: 10.5, color: GaiaColors.muted)),
          ]),
        ),
      ),
    );
  }
}

class _Chip extends StatelessWidget {
  const _Chip(this.text, {this.muted = false});
  final String text;
  final bool muted; // a switched-off sensor
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 5),
        decoration: BoxDecoration(color: muted ? const Color(0xFFEFEFEA) : GaiaColors.plant, borderRadius: BorderRadius.circular(10)),
        child: Text(text, style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: muted ? GaiaColors.muted : GaiaColors.forest)),
      );
}

class _Hint extends StatelessWidget {
  const _Hint(this.text);
  final String text;
  @override
  Widget build(BuildContext context) => Text(text, style: const TextStyle(fontSize: 12, color: GaiaColors.muted));
}
