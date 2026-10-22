import 'package:flutter/material.dart';

import '../theme/app_tokens.dart';

/// Minimal markdown for assistant answers: paragraphs, bullet / numbered lists, **bold**,
/// and citation markers like [F1] [K2] [W3] rendered as tappable chips.
class GaiaMarkdown extends StatelessWidget {
  const GaiaMarkdown(this.text, {super.key, this.onCitation, this.color = GaiaColors.ink, this.fontSize = 13});

  final String text;
  final void Function(String id)? onCitation;
  final Color color;
  final double fontSize;

  static final _token = RegExp(r'\*\*([^*]+)\*\*|\[([FKW]\d{1,3})\]');

  List<InlineSpan> _inline(String line) {
    final spans = <InlineSpan>[];
    var last = 0;
    for (final m in _token.allMatches(line)) {
      if (m.start > last) spans.add(TextSpan(text: line.substring(last, m.start)));
      if (m.group(1) != null) {
        spans.add(TextSpan(text: m.group(1), style: const TextStyle(fontWeight: FontWeight.w800)));
      } else {
        final id = m.group(2)!;
        spans.add(WidgetSpan(alignment: PlaceholderAlignment.middle, child: _CitationChip(id: id, onTap: onCitation)));
      }
      last = m.end;
    }
    if (last < line.length) spans.add(TextSpan(text: line.substring(last)));
    return spans;
  }

  @override
  Widget build(BuildContext context) {
    final style = TextStyle(color: color, fontSize: fontSize, height: 1.45);
    final blocks = <Widget>[];
    final paragraph = <String>[];

    void flush() {
      if (paragraph.isEmpty) return;
      blocks.add(Padding(
        padding: const EdgeInsets.only(bottom: 8),
        child: Text.rich(TextSpan(style: style, children: _inline(paragraph.join(' ')))),
      ));
      paragraph.clear();
    }

    for (final raw in text.replaceAll('\r', '').split('\n')) {
      final line = raw.trim();
      final bullet = RegExp(r'^([-*•]|\d+[.)])\s+(.*)$').firstMatch(line);
      final heading = RegExp(r'^#{1,4}\s+(.*)$').firstMatch(line);
      if (line.isEmpty) {
        flush();
      } else if (heading != null) {
        flush();
        blocks.add(Padding(
          padding: const EdgeInsets.only(bottom: 6),
          child: Text.rich(TextSpan(style: style.copyWith(fontWeight: FontWeight.w800), children: _inline(heading.group(1)!))),
        ));
      } else if (bullet != null) {
        flush();
        final marker = bullet.group(1)!;
        blocks.add(Padding(
          padding: const EdgeInsets.only(bottom: 5, left: 2),
          child: Row(crossAxisAlignment: CrossAxisAlignment.start, children: [
            SizedBox(width: 18, child: Text(RegExp(r'\d').hasMatch(marker) ? marker : '•', style: style)),
            Expanded(child: Text.rich(TextSpan(style: style, children: _inline(bullet.group(2)!)))),
          ]),
        ));
      } else {
        paragraph.add(line);
      }
    }
    flush();
    return Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: blocks);
  }
}

class _CitationChip extends StatelessWidget {
  const _CitationChip({required this.id, this.onTap});

  final String id;
  final void Function(String id)? onTap;

  @override
  Widget build(BuildContext context) {
    final (bg, fg) = switch (id[0]) {
      'K' => (const Color(0xFFE6E3F3), const Color(0xFF4B3F86)),
      'W' => (GaiaColors.water, const Color(0xFF1F5A73)),
      _ => (const Color(0xFFDCEBD5), GaiaColors.leaf),
    };
    return GestureDetector(
      onTap: onTap == null ? null : () => onTap!(id),
      child: Container(
        margin: const EdgeInsets.symmetric(horizontal: 1.5),
        padding: const EdgeInsets.symmetric(horizontal: 5, vertical: 1),
        decoration: BoxDecoration(color: bg, borderRadius: BorderRadius.circular(6)),
        child: Text(id, style: TextStyle(fontSize: 10, fontWeight: FontWeight.w800, color: fg)),
      ),
    );
  }
}
