import 'package:flutter/material.dart';
import 'package:url_launcher/url_launcher.dart';

import '../../app/app_services.dart';
import '../../core/network/api_client.dart';
import '../../core/theme/app_tokens.dart';
import '../../core/widgets/gaia_markdown.dart';
import '../../core/widgets/gaia_widgets.dart';
import '../../models/ai_models.dart';
import '../../services/farm_assistant_repository.dart';

class ChatPage extends StatefulWidget {
  const ChatPage({super.key});
  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  static const _suggestions = {
    ChatMode.farm: ['How humid was my field this morning?', 'Was the soil moisture lower than yesterday?', 'What crop problems have been identified?', 'Summarise my field today'],
    ChatMode.open: ['How do I prevent early blight in tomatoes?', 'Best time to sow wheat?', 'How much water does drip irrigation save?'],
  };

  final input = TextEditingController();
  final scroll = ScrollController();
  final messages = <ChatMessage>[];
  ChatMode mode = ChatMode.farm;
  bool sending = false;
  String? sessionId;
  String draft = ''; // answer text received so far for the in-flight question
  String stageLabel = 'Connecting…';
  bool _restored = false;

  @override
  void didChangeDependencies() {
    super.didChangeDependencies();
    if (_restored) return;
    _restored = true;
    _restore();
  }

  ChatMessage get _greeting => ChatMessage(
        role: ChatRole.gaia,
        text: mode == ChatMode.farm
            ? 'Hello! I’m grounded in your sensor log and farm knowledge store. Ask me anything about your field.'
            : 'Open-ended mode: I’ll search the web and cite sources. For questions about your own field, switch to “Query my farm”.',
      );

  Future<void> _restore() async {
    final services = GaiaScope.of(context);
    sessionId = services.config.chatSessionId;
    setState(() => messages.add(_greeting));
    if (sessionId == null) return;
    try {
      final previous = await services.assistant.history(sessionId!);
      if (mounted && previous.isNotEmpty) setState(() => messages.addAll(previous));
      _scrollToEnd();
    } on ApiException {
      sessionId = null;
      await services.config.setChatSession(null);
    }
  }

  Future<void> _newChat() async {
    await GaiaScope.of(context).config.setChatSession(null);
    setState(() {
      sessionId = null;
      messages
        ..clear()
        ..add(_greeting);
    });
  }

  void _scrollToEnd() => WidgetsBinding.instance.addPostFrameCallback((_) {
        if (scroll.hasClients) scroll.animateTo(scroll.position.maxScrollExtent, duration: const Duration(milliseconds: 300), curve: Curves.easeOut);
      });

  Future<void> _send([String? text]) async {
    final question = (text ?? input.text).trim();
    if (question.isEmpty || sending) return;
    final services = GaiaScope.of(context);
    setState(() {
      messages.add(ChatMessage(role: ChatRole.user, text: question));
      input.clear();
      sending = true;
      draft = '';
      stageLabel = 'Connecting…';
    });
    _scrollToEnd();
    try {
      await for (final event in services.assistant.askStream(question, mode: mode, sessionId: sessionId)) {
        if (!mounted) return;
        switch (event) {
          case ChatStage(:final label):
            setState(() => stageLabel = label);
          case ChatDelta(:final text):
            setState(() => draft += text);
            _scrollToEnd();
          case ChatDone(:final reply):
            sessionId = reply.sessionId;
            await services.config.setChatSession(sessionId);
            if (mounted) setState(() => messages.add(reply.message));
        }
      }
    } on ApiException catch (e) {
      if (mounted) setState(() => messages.add(ChatMessage(role: ChatRole.gaia, text: e.message, isError: true)));
    } finally {
      if (mounted) {
        setState(() {
          sending = false;
          draft = '';
        });
      }
      _scrollToEnd();
    }
  }

  void _showSource(SourceRef s) => showModalBottomSheet<void>(
        context: context,
        showDragHandle: true,
        isScrollControlled: true,
        builder: (_) => Padding(
          padding: const EdgeInsets.fromLTRB(22, 0, 22, 28),
          child: Column(mainAxisSize: MainAxisSize.min, crossAxisAlignment: CrossAxisAlignment.start, children: [
            Text('[${s.id}] ${s.kindLabel}', style: const TextStyle(fontSize: 11, fontWeight: FontWeight.w800, color: GaiaColors.leaf)),
            const SizedBox(height: 6),
            Text(s.title, style: const TextStyle(fontSize: 17, fontWeight: FontWeight.w800, color: GaiaColors.ink)),
            const SizedBox(height: 10),
            ConstrainedBox(
              constraints: BoxConstraints(maxHeight: MediaQuery.sizeOf(context).height * 0.45),
              child: SingleChildScrollView(child: Text(s.snippet, style: const TextStyle(fontSize: 13, height: 1.45, color: Color(0xFF52665A)))),
            ),
            if (s.url != null) ...[
              const SizedBox(height: 12),
              OutlinedButton.icon(
                onPressed: () => launchUrl(Uri.parse(s.url!), mode: LaunchMode.externalApplication),
                icon: const Icon(Icons.open_in_new, size: 16),
                label: Text(s.domain ?? 'Open source'),
              ),
            ],
          ]),
        ),
      );

  @override
  void dispose() {
    input.dispose();
    scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => Column(children: [
        const GaiaHeader(eyebrow: 'Pipeline A • RAG assistant', title: 'Ask Gaia\na better question.', subtitle: 'Your sensor log, rover notes, and crop insights in one place.'),
        Expanded(
          child: ListView(controller: scroll, padding: const EdgeInsets.symmetric(horizontal: GaiaSpacing.lg), children: [
            Row(children: [
              Expanded(
                child: Container(
                  padding: const EdgeInsets.all(4),
                  decoration: BoxDecoration(color: const Color(0xFFE8EFE5), borderRadius: BorderRadius.circular(15)),
                  child: Row(children: [_mode('Query my farm', ChatMode.farm), _mode('Open-ended', ChatMode.open)]),
                ),
              ),
              IconButton(tooltip: 'New conversation', onPressed: sending ? null : _newChat, icon: const Icon(Icons.add_comment_outlined, color: GaiaColors.leaf)),
            ]),
            const SizedBox(height: 18),
            ...messages.map((m) => _Bubble(message: m, onSource: _showSource, onFollowUp: _send)),
            if (sending && draft.isNotEmpty)
              _Bubble(message: ChatMessage(role: ChatRole.gaia, text: draft), onSource: _showSource, onFollowUp: _send),
            if (sending)
              Align(
                alignment: Alignment.centerLeft,
                child: Padding(
                  padding: const EdgeInsets.only(bottom: 12),
                  child: Row(mainAxisSize: MainAxisSize.min, children: [
                    const SizedBox(width: 16, height: 16, child: CircularProgressIndicator(strokeWidth: 2, color: GaiaColors.leaf)),
                    const SizedBox(width: 10),
                    Text(stageLabel, style: const TextStyle(fontSize: 12, color: GaiaColors.muted)),
                  ]),
                ),
              ),
            if (messages.length == 1 && !sending) ...[
              const SizedBox(height: 8),
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: _suggestions[mode]!.map((q) => ActionChip(label: Text(q, style: const TextStyle(fontSize: 11)), onPressed: () => _send(q))).toList(),
              ),
            ],
            const SizedBox(height: 90),
          ]),
        ),
        Container(
          padding: const EdgeInsets.fromLTRB(18, 10, 18, 16),
          decoration: const BoxDecoration(color: Colors.white, borderRadius: BorderRadius.vertical(top: Radius.circular(24))),
          child: Row(children: [
            Expanded(
              child: TextField(
                controller: input,
                minLines: 1,
                maxLines: 4,
                textInputAction: TextInputAction.send,
                onSubmitted: (_) => _send(),
                decoration: InputDecoration(hintText: mode == ChatMode.farm ? 'Ask about your field…' : 'Ask Gaia anything…'),
              ),
            ),
            const SizedBox(width: 8),
            IconButton(
              onPressed: sending ? null : _send,
              icon: sending ? const SizedBox(width: 18, height: 18, child: CircularProgressIndicator(strokeWidth: 2)) : const Icon(Icons.arrow_upward),
              style: IconButton.styleFrom(backgroundColor: GaiaColors.leaf, foregroundColor: Colors.white),
            ),
          ]),
        ),
      ]);

  Widget _mode(String label, ChatMode value) => Expanded(
        child: GestureDetector(
          onTap: () => setState(() {
            mode = value;
            if (messages.length == 1) messages[0] = _greeting;
          }),
          child: Container(
            padding: const EdgeInsets.symmetric(vertical: 11),
            decoration: BoxDecoration(color: mode == value ? Colors.white : Colors.transparent, borderRadius: BorderRadius.circular(12)),
            child: Text(label,
                textAlign: TextAlign.center,
                style: TextStyle(fontSize: 12, fontWeight: FontWeight.w800, color: mode == value ? GaiaColors.leaf : GaiaColors.muted)),
          ),
        ),
      );
}

class _Bubble extends StatelessWidget {
  const _Bubble({required this.message, required this.onSource, required this.onFollowUp});
  final ChatMessage message;
  final void Function(SourceRef) onSource;
  final void Function(String) onFollowUp;

  @override
  Widget build(BuildContext context) {
    final gaia = message.role == ChatRole.gaia;
    final byId = {for (final s in message.sources) s.id: s};
    final g = message.grounding;
    final badges = [
      if (g.farmData) ('From your sensor data', true),
      if (g.knowledgeStore) ('From your knowledge store', true),
      if (g.web) ('Web sources', true),
      if (g.generalKnowledge) ('Includes general knowledge', false),
      if (g.insufficientData) ('Data incomplete', false),
      if (gaia && !message.llmUsed) ('AI model offline', false),
    ];
    return Align(
      alignment: gaia ? Alignment.centerLeft : Alignment.centerRight,
      child: Container(
        margin: const EdgeInsets.only(bottom: 12),
        padding: const EdgeInsets.all(15),
        constraints: const BoxConstraints(maxWidth: 340),
        decoration: BoxDecoration(
          color: message.isError ? const Color(0xFFF8E6DC) : (gaia ? const Color(0xFFEAF2E6) : GaiaColors.leaf),
          borderRadius: const BorderRadius.only(
            topLeft: Radius.circular(18),
            topRight: Radius.circular(18),
            bottomLeft: Radius.circular(4),
            bottomRight: Radius.circular(18),
          ),
        ),
        child: Column(crossAxisAlignment: CrossAxisAlignment.start, children: [
          if (gaia)
            GaiaMarkdown(message.text, onCitation: (id) {
              final s = byId[id];
              if (s != null) onSource(s);
            })
          else
            Text(message.text, style: const TextStyle(color: Colors.white, fontSize: 13, height: 1.4)),
          if (badges.isNotEmpty) ...[
            const SizedBox(height: 10),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: badges
                  .map((b) => Container(
                        padding: const EdgeInsets.symmetric(horizontal: 8, vertical: 3),
                        decoration: BoxDecoration(color: b.$2 ? const Color(0xFFD6E8CD) : const Color(0xFFF4E7C2), borderRadius: BorderRadius.circular(8)),
                        child: Text(b.$1, style: TextStyle(fontSize: 10, fontWeight: FontWeight.w800, color: b.$2 ? GaiaColors.leaf : const Color(0xFF7A5A12))),
                      ))
                  .toList(),
            ),
          ],
          if (message.sources.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: message.sources
                  .map((s) => ActionChip(
                        visualDensity: VisualDensity.compact,
                        avatar: Icon(s.kind == 'web' ? Icons.public : (s.kind == 'knowledge' ? Icons.menu_book_outlined : Icons.sensors), size: 14),
                        label: Text('${s.id} · ${s.title}', maxLines: 1, overflow: TextOverflow.ellipsis, style: const TextStyle(fontSize: 10.5)),
                        onPressed: () => onSource(s),
                      ))
                  .toList(),
            ),
          ],
          for (final n in message.notices) Padding(padding: const EdgeInsets.only(top: 6), child: Text(n, style: const TextStyle(fontSize: 10.5, color: GaiaColors.muted))),
          if (message.followUps.isNotEmpty) ...[
            const SizedBox(height: 8),
            Wrap(
              spacing: 6,
              runSpacing: 6,
              children: message.followUps.map((q) => ActionChip(label: Text(q, style: const TextStyle(fontSize: 10.5)), onPressed: () => onFollowUp(q))).toList(),
            ),
          ],
        ]),
      ),
    );
  }
}
