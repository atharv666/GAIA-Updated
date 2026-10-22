import 'dart:async';
import 'dart:typed_data';

import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../app/app_services.dart';
import '../../core/network/api_client.dart';
import '../../core/theme/app_tokens.dart';
import '../../core/widgets/gaia_markdown.dart';
import '../../core/widgets/gaia_widgets.dart';
import '../../models/ai_models.dart';

class DiagnosisPage extends StatefulWidget {
  const DiagnosisPage({super.key});
  @override
  State<DiagnosisPage> createState() => _DiagnosisPageState();
}

class _DiagnosisPageState extends State<DiagnosisPage> {
  final note = TextEditingController();
  Uint8List? image;
  String source = 'phone';
  String filename = 'photo.jpg';
  DiagnosisResult? result;
  String? error;
  bool submitting = false;
  List<DiagnosisSummary> history = const [];
  StreamSubscription<DiagnosisResult>? _watch;
  AppServices? _services;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    final services = GaiaScope.of(context);
    if (!identical(services, _services)) {
      _services?.pendingCapture.removeListener(_onCapture);
      _services = services..pendingCapture.addListener(_onCapture);
      _loadHistory();
      _onCapture();
    }
  }

  @override
  void dispose() {
    _services?.pendingCapture.removeListener(_onCapture);
    _watch?.cancel();
    note.dispose();
    super.dispose();
  }

  /// A capture arrived from the Rover tab: show it and start the pipeline straight away.
  void _onCapture() {
    final capture = _services?.pendingCapture.value;
    if (capture == null) return;
    _services!.pendingCapture.value = null;
    setState(() {
      image = capture.bytes;
      source = capture.source;
      filename = capture.filename;
      result = null;
      error = null;
    });
    _submit();
  }

  Future<void> _loadHistory() async {
    try {
      final items = await _services!.diagnosis.recent();
      if (mounted) setState(() => history = items);
    } on ApiException {
      // The history list is optional; errors surface when the user submits.
    }
  }

  Future<void> _pick(ImageSource from) async {
    final file = await ImagePicker().pickImage(source: from, maxWidth: 2048, imageQuality: 92);
    if (file == null) return;
    final bytes = await file.readAsBytes();
    setState(() {
      image = bytes;
      source = 'phone';
      filename = file.name;
      result = null;
      error = null;
    });
  }

  Future<void> _captureRover() async {
    try {
      final bytes = await _services!.rover.captureStill();
      setState(() {
        image = bytes;
        source = 'rover';
        filename = 'rover.jpg';
        result = null;
        error = null;
      });
    } on ApiException catch (e) {
      setState(() => error = e.message);
    }
  }

  Future<void> _submit() async {
    final bytes = image;
    if (bytes == null) return;
    setState(() {
      submitting = true;
      error = null;
    });
    try {
      final job = await _services!.diagnosis.submit(bytes, filename: filename, source: source, note: note.text);
      _follow(job.id);
    } on ApiException catch (e) {
      setState(() {
        submitting = false;
        error = e.message;
      });
    }
  }

  void _follow(String id) {
    _watch?.cancel();
    setState(() => submitting = true);
    _watch = _services!.diagnosis.watch(id).listen(
      (d) => setState(() {
        result = d;
        submitting = d.isProcessing;
        if (!d.isProcessing) _loadHistory();
      }),
      onError: (Object e) => setState(() {
        submitting = false;
        error = e is ApiException ? e.message : '$e';
      }),
    );
  }

  @override
  Widget build(BuildContext context) {
    final r = result;
    return SingleChildScrollView(
      child: Column(children: [
        const GaiaHeader(
          eyebrow: 'Pipeline B • VLM + agent',
          title: 'Understand your\ncrops visually.',
          subtitle: 'Capture a leaf and let Gaia investigate the story behind it.',
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: GaiaSpacing.lg),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            _ImagePanel(image: image, busy: submitting, stage: r?.stage, onTap: submitting ? null : () => _pick(ImageSource.camera)),
            const SizedBox(height: 12),
            Wrap(spacing: 8, runSpacing: 8, children: [
              FilledButton.tonalIcon(onPressed: submitting ? null : () => _pick(ImageSource.camera), icon: const Icon(Icons.photo_camera_outlined), label: const Text('Camera')),
              FilledButton.tonalIcon(onPressed: submitting ? null : () => _pick(ImageSource.gallery), icon: const Icon(Icons.photo_library_outlined), label: const Text('Gallery')),
              FilledButton.tonalIcon(onPressed: submitting ? null : _captureRover, icon: const Icon(Icons.toys_outlined), label: const Text('Rover')),
            ]),
            const SizedBox(height: 12),
            TextField(
              controller: note,
              decoration: const InputDecoration(hintText: 'Location or note (optional, e.g. north plot row 3)', isDense: true),
            ),
            const SizedBox(height: 12),
            SizedBox(
              width: double.infinity,
              child: FilledButton.icon(
                onPressed: image == null || submitting ? null : _submit,
                icon: const Icon(Icons.center_focus_strong),
                label: Text(submitting ? 'Analysing…' : 'Analyse photo'),
              ),
            ),
            const SizedBox(height: 16),
            if (error != null) ...[ErrorPanel(message: error!), const SizedBox(height: 16)],
            _PipelineSteps(stage: r?.stage, status: r?.status),
            if (r != null && !r.isProcessing) ...[
              const SizedBox(height: 16),
              _ResultView(result: r),
            ],
            if (history.isNotEmpty) ...[
              const SizedBox(height: 24),
              const SectionTitle('Recent diagnoses', caption: 'Written to your farm knowledge store'),
              const SizedBox(height: 8),
              ...history.map((h) => _HistoryTile(item: h, onTap: () => _follow(h.id))),
            ],
            const SizedBox(height: 28),
          ]),
        ),
      ]),
    );
  }
}

class _ImagePanel extends StatelessWidget {
  const _ImagePanel({required this.image, required this.busy, required this.stage, this.onTap});
  final Uint8List? image;
  final bool busy;
  final String? stage;
  final VoidCallback? onTap;

  @override
  Widget build(BuildContext context) {
    final label = switch (stage) {
      'queued' => 'Queued…',
      'vlm' => 'The vision model is examining the leaf…',
      'searching' => 'The agent is researching trusted sources…',
      'writing' => 'Saving findings to your knowledge store…',
      _ => 'Working…',
    };
    return GestureDetector(
      onTap: onTap,
      child: ClipRRect(
        borderRadius: BorderRadius.circular(GaiaRadius.card),
        child: Container(
          height: 240,
          width: double.infinity,
          color: const Color(0xFFDDE9D9),
          child: Stack(fit: StackFit.expand, children: [
            if (image != null) Image.memory(image!, fit: BoxFit.cover),
            if (image == null)
              Column(mainAxisAlignment: MainAxisAlignment.center, children: [
                Container(
                  padding: const EdgeInsets.all(19),
                  decoration: const BoxDecoration(color: Colors.white, shape: BoxShape.circle),
                  child: const Icon(Icons.add_a_photo_outlined, color: GaiaColors.leaf, size: 30),
                ),
                const SizedBox(height: 14),
                const Text('Tap to add a crop image', style: TextStyle(fontSize: 16, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
                const SizedBox(height: 5),
                const Text('Rover capture or phone camera', style: TextStyle(fontSize: 12, color: GaiaColors.muted)),
              ]),
            if (busy)
              Container(
                color: Colors.black.withValues(alpha: .45),
                child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [
                  const CircularProgressIndicator(color: GaiaColors.lime),
                  const SizedBox(height: 16),
                  Text(label, style: const TextStyle(fontWeight: FontWeight.w800, color: Colors.white)),
                ]),
              ),
          ]),
        ),
      ),
    );
  }
}

class _PipelineSteps extends StatelessWidget {
  const _PipelineSteps({required this.stage, required this.status});
  final String? stage;
  final String? status;

  static const _order = ['vlm', 'searching', 'writing', 'done'];

  @override
  Widget build(BuildContext context) {
    final idx = stage == null ? -1 : _order.indexOf(stage!);
    final failed = status == 'failed';
    String state(int i) {
      if (idx < 0) return i == 0 ? 'READY' : '';
      if (failed && i == idx.clamp(0, 2)) return 'FAILED';
      if (i < idx || stage == 'done' && status == 'completed') return 'DONE';
      return i == idx ? 'RUNNING' : '';
    }

    return Column(children: [
      _Step(number: 1, icon: Icons.visibility_outlined, title: 'VLM visual analysis', subtitle: 'Plant • health • evidence • confidence', state: state(0)),
      _Step(number: 2, icon: Icons.travel_explore, title: 'Agent search', subtitle: 'Find relevant care and treatment context', state: state(1)),
      _Step(number: 3, icon: Icons.library_add_outlined, title: 'Present + augment', subtitle: 'Write findings to Gaia’s shared knowledge store', state: state(2)),
    ]);
  }
}

class _Step extends StatelessWidget {
  final int number;
  final IconData icon;
  final String title, subtitle, state;
  const _Step({required this.number, required this.icon, required this.title, required this.subtitle, required this.state});
  @override
  Widget build(BuildContext context) {
    final active = state == 'RUNNING' || state == 'READY';
    final done = state == 'DONE';
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(color: active ? const Color(0xFFEDF5E8) : Colors.white, borderRadius: BorderRadius.circular(18)),
      child: Row(children: [
        CircleAvatar(
          radius: 15,
          backgroundColor: done ? GaiaColors.lime : (active ? GaiaColors.leaf : GaiaColors.line),
          child: done
              ? const Icon(Icons.check, size: 16, color: GaiaColors.forest)
              : Text('$number', style: TextStyle(color: active ? Colors.white : GaiaColors.muted, fontSize: 12, fontWeight: FontWeight.bold)),
        ),
        const SizedBox(width: 12),
        Icon(icon, color: GaiaColors.leaf),
        const SizedBox(width: 10),
        Expanded(
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text(title, style: const TextStyle(fontWeight: FontWeight.w800, color: GaiaColors.ink)),
            const SizedBox(height: 3),
            Text(subtitle, style: const TextStyle(fontSize: 11, color: GaiaColors.muted)),
          ]),
        ),
        if (state.isNotEmpty)
          Text(state, style: TextStyle(fontSize: 10, fontWeight: FontWeight.bold, color: state == 'FAILED' ? GaiaColors.terracotta : GaiaColors.leaf)),
      ]),
    );
  }
}

class _ResultView extends StatelessWidget {
  const _ResultView({required this.result});
  final DiagnosisResult result;

  Widget _bullets(List<String> items) => Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: items
            .map((i) => Padding(
                  padding: const EdgeInsets.only(bottom: 4),
                  child: GaiaMarkdown('- $i', fontSize: 12.5, color: const Color(0xFF52665A)),
                ))
            .toList(),
      );

  @override
  Widget build(BuildContext context) {
    final r = result;
    if (r.status == 'failed') return ErrorPanel(message: 'Diagnosis failed: ${r.error ?? 'unknown error'}');
    final a = r.analysis;
    if (a == null) return const SizedBox.shrink();
    final cited = r.sources.where((s) => s.cited).toList();
    return Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
      Container(
        padding: const EdgeInsets.all(18),
        decoration: BoxDecoration(color: const Color(0xFFE9F2E4), borderRadius: BorderRadius.circular(22)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [
            Expanded(
              child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                Text(a.plant, style: const TextStyle(fontSize: 19, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
                if (a.scientificName != null || a.plantPart != null)
                  Text([a.scientificName, a.plantPart].whereType<String>().join(' · '), style: const TextStyle(fontSize: 11, color: GaiaColors.muted)),
              ]),
            ),
            HealthPill(a.status),
          ]),
          if (r.status == 'not_plant') ...[
            const SizedBox(height: 10),
            const Text('This photo does not seem to show a plant, so nothing was researched or stored.', style: TextStyle(fontSize: 12.5, color: GaiaColors.terracotta)),
          ],
          const SizedBox(height: 12),
          const Text('POSSIBLE ISSUE', style: TextStyle(fontSize: 10, fontWeight: FontWeight.w800, letterSpacing: 1.2, color: GaiaColors.muted)),
          const SizedBox(height: 4),
          Text(a.diagnosis, style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
          if (a.issueCategories.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(spacing: 6, runSpacing: 6, children: a.issueCategories.map((c) => Chip(label: Text(c.replaceAll('_', ' '), style: const TextStyle(fontSize: 10.5)), visualDensity: VisualDensity.compact)).toList()),
          ],
          const SizedBox(height: 12),
          const Text('VISUAL EVIDENCE', style: TextStyle(fontSize: 10, fontWeight: FontWeight.w800, letterSpacing: 1.2, color: GaiaColors.muted)),
          const SizedBox(height: 4),
          _bullets(a.evidence),
          if (a.differential.isNotEmpty) ...[
            const SizedBox(height: 8),
            const Text('COULD ALSO BE', style: TextStyle(fontSize: 10, fontWeight: FontWeight.w800, letterSpacing: 1.2, color: GaiaColors.muted)),
            const SizedBox(height: 4),
            _bullets(a.differential),
          ],
          const SizedBox(height: 14),
          Row(children: [
            const Icon(Icons.analytics_outlined, size: 16, color: GaiaColors.leaf),
            const SizedBox(width: 6),
            Text('Model confidence: ${(a.confidence * 100).round()}%  •  rough indicator',
                style: const TextStyle(fontSize: 11, color: GaiaColors.leaf, fontWeight: FontWeight.w700)),
          ]),
          const SizedBox(height: 6),
          ClipRRect(
            borderRadius: BorderRadius.circular(6),
            child: LinearProgressIndicator(value: a.confidence, minHeight: 6, backgroundColor: Colors.white70, color: GaiaColors.leaf),
          ),
          const SizedBox(height: 6),
          Text(r.confidenceNote, style: const TextStyle(fontSize: 10.5, color: GaiaColors.muted)),
        ]),
      ),
      if (r.recaptureTips.isNotEmpty) ...[
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.all(14),
          decoration: BoxDecoration(color: const Color(0xFFFBF2D9), borderRadius: BorderRadius.circular(18)),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            const Text('For a clearer assessment, try recapturing', style: TextStyle(fontWeight: FontWeight.w800, color: Color(0xFF6B4E0F))),
            const SizedBox(height: 6),
            _bullets(r.recaptureTips),
          ]),
        ),
      ],
      if (r.findings != null) ...[
        const SizedBox(height: 16),
        Row(children: [
          const Expanded(child: Text('Agent-retrieved context', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 16, color: GaiaColors.ink))),
          Text(r.webGrounded ? '${cited.length} web sources' : 'general knowledge',
              style: TextStyle(fontSize: 11, fontWeight: FontWeight.w700, color: r.webGrounded ? GaiaColors.leaf : GaiaColors.terracotta)),
        ]),
        const SizedBox(height: 8),
        Container(
          width: double.infinity,
          padding: const EdgeInsets.all(15),
          decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(18)),
          child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
            GaiaMarkdown(r.findings!.headline, fontSize: 13),
            for (final (title, items) in r.findings!.sections) ...[
              const SizedBox(height: 8),
              Text(title, style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 13, color: GaiaColors.ink)),
              const SizedBox(height: 4),
              _bullets(items),
            ],
            if (cited.isNotEmpty) ...[
              const SizedBox(height: 10),
              const Text('Sources', style: TextStyle(fontWeight: FontWeight.w800, fontSize: 13, color: GaiaColors.ink)),
              for (final s in cited)
                ListTile(
                  dense: true,
                  contentPadding: EdgeInsets.zero,
                  leading: Icon(s.quality == 'institutional' ? Icons.verified_outlined : Icons.link, size: 18, color: GaiaColors.leaf),
                  title: Text('[${s.id}] ${s.title}', maxLines: 2, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12)),
                  subtitle: Text('${s.domain} · ${s.quality.replaceAll('_', ' ')} source', style: const TextStyle(fontSize: 10.5)),
                  onTap: () => launchUrl(Uri.parse(s.url), mode: LaunchMode.externalApplication),
                ),
            ],
          ]),
        ),
      ],
      if (r.knowledgeEntries > 0) ...[
        const SizedBox(height: 10),
        Text('Added to your farm knowledge store (${r.knowledgeEntries} entries) — ask about it in “Query my farm”.',
            style: const TextStyle(fontSize: 11.5, color: GaiaColors.leaf, fontWeight: FontWeight.w700)),
      ],
      const SizedBox(height: 10),
      Text(r.disclaimer, style: const TextStyle(fontSize: 10.5, color: GaiaColors.muted)),
    ]);
  }
}

class _HistoryTile extends StatelessWidget {
  const _HistoryTile({required this.item, required this.onTap});
  final DiagnosisSummary item;
  final VoidCallback onTap;
  @override
  Widget build(BuildContext context) {
    final services = GaiaScope.of(context);
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(16)),
      child: ListTile(
        onTap: onTap,
        leading: ClipRRect(
          borderRadius: BorderRadius.circular(10),
          child: Image.network(
            services.diagnosis.imageUrl(item.imageUrl),
            headers: services.diagnosis.imageHeaders,
            width: 46,
            height: 46,
            fit: BoxFit.cover,
            errorBuilder: (_, __, ___) => Container(width: 46, height: 46, color: GaiaColors.plant, child: const Icon(Icons.eco, color: GaiaColors.leaf)),
          ),
        ),
        title: Text(item.plant ?? 'Processing…', style: const TextStyle(fontWeight: FontWeight.w800, fontSize: 13)),
        subtitle: Text(item.diagnosis ?? item.status, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 11.5)),
        trailing: item.health == null ? null : HealthPill(item.health!),
      ),
    );
  }
}
