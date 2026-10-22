import 'dart:convert';

import '../core/network/api_client.dart';
import '../models/ai_models.dart';
import '../models/json_utils.dart';

/// Progress of a streamed answer.
sealed class ChatStreamEvent {
  const ChatStreamEvent();
}

/// The pipeline entered a stage (`syncing`, `planning`, `retrieving`, `searching`, `generating`).
class ChatStage extends ChatStreamEvent {
  const ChatStage(this.name);
  final String name;

  String get label => switch (name) {
        'syncing' => 'Syncing the latest sensor data…',
        'planning' => 'Understanding your question…',
        'retrieving' => 'Looking through your farm data…',
        'searching' => 'Searching the web…',
        'generating' => 'Writing the answer…',
        _ => 'Working…',
      };
}

/// More answer text (a draft: citations are validated in the final event).
class ChatDelta extends ChatStreamEvent {
  const ChatDelta(this.text);
  final String text;
}

/// The finished, citation-validated reply.
class ChatDone extends ChatStreamEvent {
  const ChatDone(this.reply);
  final ChatReply reply;
}

/// Pipeline A: "Query my farm" (sensor + knowledge RAG) and open-ended (web-grounded) chat.
class FarmAssistantRepository {
  FarmAssistantRepository(this._api);

  final GaiaApiClient _api;

  Future<ChatReply> ask(String message, {required ChatMode mode, String? sessionId}) async {
    final data = await _api.postJson('/chat', {
      'message': message,
      'mode': mode == ChatMode.farm ? 'farm' : 'open',
      if (sessionId != null) 'session_id': sessionId,
    });
    return ChatReply.fromJson(map(data));
  }

  /// Streams the answer (`POST /chat/stream`): stage updates, then text as it is written, then the final reply.
  ///
  /// Falls back to the plain endpoint if the server predates streaming.
  Stream<ChatStreamEvent> askStream(String message, {required ChatMode mode, String? sessionId}) async* {
    final body = {
      'message': message,
      'mode': mode == ChatMode.farm ? 'farm' : 'open',
      if (sessionId != null) 'session_id': sessionId,
    };
    var received = false;
    try {
      await for (final event in _api.postSse('/chat/stream', body)) {
        received = true;
        final data = map(jsonDecode(event.data));
        switch (event.event) {
          case 'stage':
            yield ChatStage(str(data['name']));
          case 'delta':
            yield ChatDelta(str(data['text']));
          case 'final':
            yield ChatDone(ChatReply.fromJson(data));
            return;
          case 'error':
            throw ApiException(str(data['message'], 'The AI service could not answer right now.'));
        }
      }
      if (!received) throw const ApiException('The server closed the connection without an answer.');
      throw const ApiException('The answer was interrupted. Please try again.');
    } on ApiException catch (e) {
      if (e.statusCode == 404 && !received) {
        yield ChatDone(await ask(message, mode: mode, sessionId: sessionId));
        return;
      }
      rethrow;
    }
  }

  /// Restores a previous conversation (messages + their sources).
  Future<List<ChatMessage>> history(String sessionId) async {
    final data = map(await _api.getJson('/chat/sessions/$sessionId'));
    return maps(data['messages']).map((m) {
      final isUser = m['role'] == 'user';
      return ChatMessage(
        role: isUser ? ChatRole.user : ChatRole.gaia,
        text: str(m['content']),
        sources: isUser ? const [] : maps(m['sources']).map(SourceRef.fromJson).toList(),
        grounding: isUser ? const Grounding() : Grounding.fromJson(map(m['grounding'])),
      );
    }).toList();
  }
}
