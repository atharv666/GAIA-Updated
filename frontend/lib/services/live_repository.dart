import 'dart:convert';

import '../core/network/api_client.dart';
import '../models/json_utils.dart';
import '../models/live_models.dart';

/// Live view: a snapshot of the running system plus a stream of what it is doing.
class LiveRepository {
  LiveRepository(this._api);

  final GaiaApiClient _api;

  Future<LiveState> state({int events = 40}) async =>
      LiveState.fromJson(map(await _api.getJson('/live/state', query: {'events': '$events'})));

  /// Events after [after] (or only new ones when null). Ends when the connection drops; callers reconnect.
  Stream<LiveEvent> events({int? after}) async* {
    await for (final e in _api.getSse('/live/stream', query: {if (after != null) 'after': '$after'})) {
      if (e.event == 'hello') {
        // Connection confirmed; lets the UI show "live" even while nothing is happening.
        yield LiveEvent(id: integer(map(jsonDecode(e.data))['last_event_id']), at: DateTime.now(), kind: 'hello', title: '', data: const {});
      } else if (e.event == 'activity') {
        yield LiveEvent.fromJson(map(jsonDecode(e.data)));
      }
    }
  }
}
