import 'package:flutter/material.dart';

import '../../app/app_services.dart';
import '../../core/constants/app_constants.dart';
import '../../core/network/api_client.dart';
import '../../core/theme/app_tokens.dart';
import '../../core/widgets/gaia_widgets.dart';
import '../../models/ai_models.dart';
import '../../models/json_utils.dart';
import '../../models/sensor_reading.dart';

class DashboardPage extends StatefulWidget {
  const DashboardPage({super.key, this.active = true});
  final bool active;
  @override
  State<DashboardPage> createState() => _DashboardPageState();
}

class _DashboardPageState extends State<DashboardPage> {
  List<SensorReading> readings = const [];
  List<SensorSummary> today = const [];
  List<DiagnosisSummary> diagnoses = const [];
  FarmOverview? overview;
  String farmer = 'there';
  String? error;
  bool loading = true;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (loading && overview == null && error == null) _load();
  }

  @override
  void didUpdateWidget(DashboardPage old) {
    super.didUpdateWidget(old);
    if (widget.active && !old.active) _load();
  }

  Future<void> _load() async {
    final s = GaiaScope.of(context);
    try {
      final results = await Future.wait([
        s.sensors.latest(),
        s.sensors.summary(range: 'day:0'),
        s.diagnosis.recent(limit: 3),
        s.sensors.overview(),
        s.api.getJson('/config'),
      ]);
      if (!mounted) return;
      setState(() {
        readings = (results[0] as List<SensorReading>).where((r) => r.rag).toList();
        today = (results[1] as List<SensorSummary>).where((r) => sensorKindOf(r.sensor) != SensorKind.rover).toList();
        diagnoses = results[2] as List<DiagnosisSummary>;
        overview = results[3] as FarmOverview;
        farmer = str(map(results[4])['farmer_name'], 'there');
        error = null;
        loading = false;
      });
    } on ApiException catch (e) {
      if (mounted) {
        setState(() {
          error = e.message;
          loading = false;
        });
      }
    }
  }

  String get _greeting {
    final h = DateTime.now().hour;
    return h < 12 ? 'Good morning' : (h < 17 ? 'Good afternoon' : 'Good evening');
  }

  String _date(DateTime d) {
    const days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];
    const months = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
    return '${days[d.weekday - 1]}, ${d.day} ${months[d.month - 1]} ${d.year}';
  }

  @override
  Widget build(BuildContext context) {
    final alerts = readings.where((r) => r.isAlert).toList();
    return RefreshIndicator(
      onRefresh: _load,
      child: ListView(padding: EdgeInsets.zero, children: [
        GaiaHeader(
          eyebrow: _date(DateTime.now()),
          title: '$_greeting, $farmer.',
          subtitle: alerts.isEmpty ? 'Your farm is looking steady today.' : alerts.map((a) => '${a.label}: ${a.band}').join(' · '),
        ),
        Padding(
          padding: const EdgeInsets.symmetric(horizontal: GaiaSpacing.lg),
          child: Column(children: [
            if (error != null) ...[ErrorPanel(message: error!, onRetry: _load), const SizedBox(height: 16)],
            FadeIn(child: _StatusHero(overview: overview, alerts: alerts.length, loading: loading)),
            const SizedBox(height: 16),
            if (readings.isNotEmpty)
              FadeIn(
                delay: 60,
                child: GridView.count(
                  crossAxisCount: 3,
                  shrinkWrap: true,
                  physics: const NeverScrollableScrollPhysics(),
                  mainAxisSpacing: 10,
                  crossAxisSpacing: 10,
                  childAspectRatio: 0.95,
                  children: readings.take(6).map((r) => SensorMetricCard(reading: r)).toList(),
                ),
              ),
            if (today.isNotEmpty) ...[
              const SizedBox(height: 22),
              const SectionTitle('Today so far', caption: 'Exact statistics from your sensor log'),
              const SizedBox(height: 10),
              _TodayCard(stats: today),
            ],
            const SizedBox(height: 22),
            const SectionTitle('Field intelligence', caption: 'Latest crop diagnoses'),
            const SizedBox(height: 10),
            if (diagnoses.isEmpty)
              _InsightCard(
                icon: Icons.center_focus_strong,
                title: 'No diagnoses yet',
                body: 'Capture a leaf from the rover or your phone to start building crop knowledge.',
                action: 'Open Diagnose',
                color: const Color(0xFFEDF5E8),
                onTap: () => GaiaScope.of(context).requestedTab.value = GaiaRoutes.diagnosis,
              )
            else
              ...diagnoses.map((d) => Padding(
                    padding: const EdgeInsets.only(bottom: 10),
                    child: _InsightCard(
                      icon: Icons.eco,
                      title: '${d.plant ?? 'Crop'} · ${d.health?.name ?? d.status}',
                      body: d.diagnosis ?? 'Processing…',
                      action: 'View in Diagnose',
                      color: d.health == HealthStatus.healthy ? const Color(0xFFEDF5E8) : const Color(0xFFF7EDE4),
                      onTap: () => GaiaScope.of(context).requestedTab.value = GaiaRoutes.diagnosis,
                    ),
                  )),
            const SizedBox(height: 12),
            _InsightCard(
              icon: Icons.auto_awesome,
              title: 'Ask about your field',
              body: overview == null
                  ? 'Gaia answers from your own sensor log and knowledge store.'
                  : '${overview!.knowledgeTotal} knowledge entries and ${overview!.readings} readings ready to query.',
              action: 'Open Gaia AI',
              color: const Color(0xFFEAF1F4),
              onTap: () => GaiaScope.of(context).requestedTab.value = GaiaRoutes.chat,
            ),
            const SizedBox(height: 22),
            const SectionTitle('Your AI pipelines', caption: 'Two independent generative flows'),
            const SizedBox(height: 10),
            const Row(children: [
              Expanded(child: _PipelineCard(number: '01', title: 'Query my farm', subtitle: 'RAG assistant', icon: Icons.forum_outlined, color: GaiaColors.leaf)),
              SizedBox(width: 12),
              Expanded(child: _PipelineCard(number: '02', title: 'Crop diagnosis', subtitle: 'Vision + web agent', icon: Icons.center_focus_strong, color: GaiaColors.terracotta)),
            ]),
            const SizedBox(height: 16),
            const Text(GaiaCopy.aiDisclosure, textAlign: TextAlign.center, style: TextStyle(fontSize: 10.5, color: GaiaColors.muted)),
            const SizedBox(height: 28),
          ]),
        ),
      ]),
    );
  }
}

class _StatusHero extends StatelessWidget {
  const _StatusHero({required this.overview, required this.alerts, required this.loading});
  final FarmOverview? overview;
  final int alerts;
  final bool loading;

  String _ago(DateTime t) {
    final d = DateTime.now().difference(t);
    if (d.inMinutes < 1) return 'just now';
    if (d.inMinutes < 60) return '${d.inMinutes} min ago';
    if (d.inHours < 48) return '${d.inHours} h ago';
    return '${d.inDays} days ago';
  }

  @override
  Widget build(BuildContext context) {
    final o = overview;
    final title = loading
        ? 'Checking your\nfield…'
        : o?.lastReadingAt == null
            ? 'Waiting for the\nfirst rover readings.'
            : alerts > 0
                ? '$alerts reading${alerts > 1 ? 's' : ''} need\nyour attention.'
                : 'All readings within\nexpected ranges.';
    final sub = o?.lastReadingAt == null ? 'Sensor log is empty' : 'Last reading ${_ago(o!.lastReadingAt!)}  •  ${o.readings} readings logged';
    return Container(
      padding: const EdgeInsets.all(20),
      decoration: BoxDecoration(
        gradient: const LinearGradient(colors: [GaiaColors.forest, GaiaColors.leaf]),
        borderRadius: BorderRadius.circular(GaiaRadius.card),
      ),
      child: Stack(children: [
        Positioned(right: -20, bottom: -30, child: Icon(Icons.eco, size: 160, color: Colors.white.withValues(alpha: .07))),
        Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('FIELD STATUS', style: TextStyle(color: GaiaColors.lime, letterSpacing: 1.6, fontSize: 11, fontWeight: FontWeight.bold)),
          const SizedBox(height: 10),
          Text(title, style: const TextStyle(color: Colors.white, fontSize: 26, height: 1.08, fontWeight: FontWeight.w800)),
          const SizedBox(height: 16),
          Row(children: [
            Icon(Icons.circle, size: 9, color: alerts > 0 ? const Color(0xFFF2B08A) : GaiaColors.lime),
            const SizedBox(width: 8),
            Expanded(child: Text(sub, style: const TextStyle(color: Color(0xFFDCEBD7), fontSize: 12))),
          ]),
        ]),
      ]),
    );
  }
}

class _TodayCard extends StatelessWidget {
  const _TodayCard({required this.stats});
  final List<SensorSummary> stats;

  String _fmt(SensorSummary s, double v) {
    final decimals = s.unit == 'ADC' || s.sensor == 'humidity' ? 0 : 1;
    final unit = s.unit == 'ADC' ? '' : s.unit;
    return '${v.toStringAsFixed(decimals)}$unit';
  }

  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.fromLTRB(16, 8, 16, 8),
        decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(20)),
        child: Column(
          children: stats
              .map((s) => Padding(
                    padding: const EdgeInsets.symmetric(vertical: 8),
                    child: Row(children: [
                      Icon(sensorIcon(s.sensor), size: 18, color: GaiaColors.leaf),
                      const SizedBox(width: 10),
                      Expanded(
                        child: Text(s.label, maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w700, color: GaiaColors.ink)),
                      ),
                      Text('avg ${_fmt(s, s.avg)}', style: const TextStyle(fontSize: 12.5, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
                      const SizedBox(width: 8),
                      Text('${_fmt(s, s.min)}–${_fmt(s, s.max)}', style: const TextStyle(fontSize: 11, color: GaiaColors.muted)),
                    ]),
                  ))
              .toList(),
        ),
      );
}

class _InsightCard extends StatelessWidget {
  final IconData icon;
  final String title, body, action;
  final Color color;
  final VoidCallback? onTap;
  const _InsightCard({required this.icon, required this.title, required this.body, required this.action, required this.color, this.onTap});
  @override
  Widget build(BuildContext context) => Material(
        color: color,
        borderRadius: BorderRadius.circular(20),
        child: InkWell(
          borderRadius: BorderRadius.circular(20),
          onTap: onTap,
          child: Padding(
            padding: const EdgeInsets.all(15),
            child: Row(children: [
              Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(color: Colors.white.withValues(alpha: .75), shape: BoxShape.circle),
                child: Icon(icon, color: GaiaColors.leaf, size: 20),
              ),
              const SizedBox(width: 12),
              Expanded(
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  Text(title, style: const TextStyle(fontWeight: FontWeight.w800, color: GaiaColors.ink)),
                  const SizedBox(height: 3),
                  Text(body, style: const TextStyle(fontSize: 12, color: Color(0xFF64746A))),
                  const SizedBox(height: 6),
                  Text(action, style: const TextStyle(fontSize: 11, color: GaiaColors.leaf, fontWeight: FontWeight.w800)),
                ]),
              ),
              const Icon(Icons.chevron_right, color: GaiaColors.muted),
            ]),
          ),
        ),
      );
}

class _PipelineCard extends StatelessWidget {
  final String number, title, subtitle;
  final IconData icon;
  final Color color;
  const _PipelineCard({required this.number, required this.title, required this.subtitle, required this.icon, required this.color});
  @override
  Widget build(BuildContext context) => Container(
        padding: const EdgeInsets.all(15),
        decoration: BoxDecoration(color: Colors.white, borderRadius: BorderRadius.circular(20), border: Border.all(color: GaiaColors.line)),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Row(children: [Text(number, style: TextStyle(color: color, fontWeight: FontWeight.w800)), const Spacer(), Icon(icon, color: color, size: 21)]),
          const SizedBox(height: 20),
          Text(title, style: const TextStyle(fontSize: 14, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
          const SizedBox(height: 4),
          Text(subtitle, style: const TextStyle(fontSize: 10, color: GaiaColors.muted)),
        ]),
      );
}
