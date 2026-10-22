import 'package:flutter/material.dart';

import '../../app/app_services.dart';
import '../../core/theme/app_tokens.dart';

Future<void> showSettingsSheet(BuildContext context) => showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => GaiaScope(services: GaiaScope.of(context), child: const SettingsSheet()),
    );

class SettingsSheet extends StatefulWidget {
  const SettingsSheet({super.key});

  @override
  State<SettingsSheet> createState() => _SettingsSheetState();
}

class _SettingsSheetState extends State<SettingsSheet> {
  late final TextEditingController backend, rover, stream, apiKey, deviceKey;
  late bool relay;
  bool saving = false;
  bool _initialised = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_initialised) return;
    _initialised = true;
    final c = GaiaScope.of(context).config;
    backend = TextEditingController(text: c.backendUrl);
    rover = TextEditingController(text: c.roverUrl);
    stream = TextEditingController(text: c.customStreamUrl);
    apiKey = TextEditingController(text: c.apiKey);
    deviceKey = TextEditingController(text: c.deviceKey);
    relay = c.relayTelemetry;
  }

  @override
  void dispose() {
    for (final c in [backend, rover, stream, apiKey, deviceKey]) {
      c.dispose();
    }
    super.dispose();
  }

  Future<void> _save() async {
    final services = GaiaScope.of(context);
    setState(() => saving = true);
    await services.config.update(
      backendUrl: backend.text,
      roverUrl: rover.text,
      streamUrl: stream.text,
      apiKey: apiKey.text,
      deviceKey: deviceKey.text,
      relayTelemetry: relay,
    );
    await services.monitor.refresh();
    if (mounted) setState(() => saving = false);
  }

  Widget _field(String label, TextEditingController c, {String? hint, bool secret = false}) => Padding(
        padding: const EdgeInsets.only(bottom: 12),
        child: TextField(
          controller: c,
          obscureText: secret,
          autocorrect: false,
          keyboardType: secret ? TextInputType.visiblePassword : TextInputType.url,
          decoration: InputDecoration(labelText: label, hintText: hint, isDense: true),
        ),
      );

  @override
  Widget build(BuildContext context) {
    final services = GaiaScope.of(context);
    return Padding(
      padding: EdgeInsets.fromLTRB(22, 0, 22, 22 + MediaQuery.viewInsetsOf(context).bottom),
      child: SingleChildScrollView(
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          const Text('Connections', style: TextStyle(fontSize: 22, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
          const SizedBox(height: 4),
          const Text('The GAIA server runs both AI pipelines; the rover streams video and sensor data.',
              style: TextStyle(fontSize: 12, color: GaiaColors.muted)),
          const SizedBox(height: 16),
          _field('GAIA server URL', backend, hint: 'http://192.168.1.10:8000'),
          _field('Rover URL', rover, hint: 'http://192.168.4.1 (rover hotspot)'),
          _field('Rover stream URL (optional)', stream, hint: 'defaults to <rover>:81/stream'),
          _field('Server API key (if the server requires one)', apiKey, secret: true),
          _field('Device key (for relaying rover telemetry)', deviceKey, secret: true),
          SwitchListTile(
            contentPadding: EdgeInsets.zero,
            value: relay,
            onChanged: (v) => setState(() => relay = v),
            title: const Text('Relay rover telemetry', style: TextStyle(fontWeight: FontWeight.w700, fontSize: 14)),
            subtitle: const Text('Use when the phone is on the rover hotspot and the rover cannot reach the server.',
                style: TextStyle(fontSize: 11)),
          ),
          const SizedBox(height: 8),
          ListenableBuilder(
            listenable: Listenable.merge([services.monitor, services.relay]),
            builder: (context, _) {
              final m = services.monitor;
              return Container(
                width: double.infinity,
                padding: const EdgeInsets.all(14),
                decoration: BoxDecoration(color: const Color(0xFFF0F4EE), borderRadius: BorderRadius.circular(16)),
                child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
                  _status('GAIA server', m.backendOnline == true, m.backendOnline == null ? 'checking…' : m.backendError ?? 'online'),
                  if (m.backendOnline == true) ...[
                    _status('Chat model', m.llmReady, m.llmReady ? 'configured' : 'not configured on the server'),
                    _status('Vision model', m.vlmReady, m.vlmReady ? 'configured' : 'not configured on the server'),
                  ],
                  _status('Rover', m.roverOnline, m.roverOnline ? '${m.rover!.deviceId} · ${m.rover!.ip}' : m.roverError ?? 'offline'),
                  if (services.relay.running)
                    _status('Relay', services.relay.lastError == null,
                        services.relay.lastError ?? '${services.relay.forwarded} readings forwarded, ${services.relay.queued} queued'),
                ]),
              );
            },
          ),
          const SizedBox(height: 14),
          SizedBox(
            width: double.infinity,
            child: FilledButton.icon(
              onPressed: saving ? null : _save,
              icon: saving
                  ? const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: Colors.white))
                  : const Icon(Icons.check),
              label: const Text('Save and test'),
            ),
          ),
        ]),
      ),
    );
  }

  Widget _status(String label, bool ok, String detail) => Padding(
        padding: const EdgeInsets.symmetric(vertical: 3),
        child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
          Icon(ok ? Icons.check_circle : Icons.error_outline, size: 16, color: ok ? GaiaColors.leaf : GaiaColors.terracotta),
          const SizedBox(width: 8),
          SizedBox(width: 92, child: Text(label, style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w700))),
          Expanded(child: Text(detail, style: const TextStyle(fontSize: 12, color: GaiaColors.muted))),
        ]),
      );
}
