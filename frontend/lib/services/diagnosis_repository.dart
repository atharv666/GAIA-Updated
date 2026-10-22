import 'dart:async';

import '../core/network/api_client.dart';
import '../models/ai_models.dart';
import '../models/json_utils.dart';

/// Pipeline B: upload a crop photo, then follow the job through VLM -> agent -> knowledge write.
class DiagnosisRepository {
  DiagnosisRepository(this._api);

  final GaiaApiClient _api;

  Future<DiagnosisResult> submit(List<int> bytes, {required String filename, required String source, String? note}) async {
    final data = await _api.postMultipart(
      '/diagnoses',
      field: 'image',
      bytes: bytes,
      filename: filename,
      fields: {'source': source, if (note != null && note.trim().isNotEmpty) 'note': note.trim()},
    );
    return DiagnosisResult.fromJson(map(data));
  }

  Future<DiagnosisResult> get(String id) async => DiagnosisResult.fromJson(map(await _api.getJson('/diagnoses/$id')));

  Future<List<DiagnosisSummary>> recent({int limit = 10}) async {
    final data = await _api.getJson('/diagnoses', query: {'limit': '$limit'});
    return maps(data).map(DiagnosisSummary.fromJson).toList();
  }

  /// Polls a job until it reaches a terminal state, emitting every stage change.
  Stream<DiagnosisResult> watch(String id, {Duration interval = const Duration(milliseconds: 1200)}) async* {
    String? lastStage;
    while (true) {
      final d = await get(id);
      if (d.stage != lastStage || !d.isProcessing) {
        lastStage = d.stage;
        yield d;
      }
      if (!d.isProcessing) return;
      await Future<void>.delayed(interval);
    }
  }

  String imageUrl(String path) => _api.absolute(path);

  Map<String, String> get imageHeaders => _api.authHeaders;
}
