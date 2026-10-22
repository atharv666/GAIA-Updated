import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../config/app_config.dart';
import 'sse_parser.dart';

class ApiException implements Exception {
  const ApiException(this.message, {this.statusCode});

  final String message;
  final int? statusCode;

  bool get isUnreachable => statusCode == null;

  @override
  String toString() => message;
}

/// JSON client for the GAIA backend (`/api/v1`).
class GaiaApiClient {
  GaiaApiClient(this.config, {http.Client? client}) : _http = client ?? http.Client();

  final AppConfig config;
  final http.Client _http;

  Uri uri(String path, [Map<String, String>? query]) {
    final base = Uri.parse('${config.backendUrl}/api/v1$path');
    return query == null || query.isEmpty ? base : base.replace(queryParameters: query);
  }

  /// Absolute URL for backend-relative paths such as `/api/v1/diagnoses/<id>/image`.
  String absolute(String path) => path.startsWith('http') ? path : '${config.backendUrl}$path';

  Map<String, String> get authHeaders => {if (config.apiKey.isNotEmpty) 'X-API-Key': config.apiKey};

  Future<dynamic> getJson(String path, {Map<String, String>? query, Duration timeout = const Duration(seconds: 20)}) {
    return _send(() => _http.get(uri(path, query), headers: authHeaders), timeout);
  }

  Future<dynamic> postJson(
    String path,
    Object? body, {
    Map<String, String>? query,
    Map<String, String>? headers,
    Duration timeout = const Duration(seconds: 90),
  }) {
    return _send(
      () => _http.post(
        uri(path, query),
        headers: {...authHeaders, 'Content-Type': 'application/json', ...?headers},
        body: jsonEncode(body ?? const {}),
      ),
      timeout,
    );
  }

  /// POSTs JSON and yields Server-Sent Events as they arrive (used for streamed chat answers).
  ///
  /// Throws [ApiException] before the first event for HTTP errors (so callers can fall back to a plain
  /// request) and while streaming if the connection drops.
  Stream<SseEvent> postSse(String path, Object? body, {Duration connectTimeout = const Duration(seconds: 30)}) {
    final request = http.Request('POST', uri(path))
      ..headers.addAll({...authHeaders, 'Content-Type': 'application/json', 'Accept': 'text/event-stream'})
      ..body = jsonEncode(body ?? const {});
    return _sse(request, connectTimeout);
  }

  /// GETs a Server-Sent Events stream that stays open (used by the Live view).
  Stream<SseEvent> getSse(String path, {Map<String, String>? query, Duration connectTimeout = const Duration(seconds: 15)}) {
    final request = http.Request('GET', uri(path, query))..headers.addAll({...authHeaders, 'Accept': 'text/event-stream'});
    return _sse(request, connectTimeout);
  }

  Stream<SseEvent> _sse(http.Request request, Duration connectTimeout) async* {
    final http.StreamedResponse response;
    try {
      response = await _http.send(request).timeout(connectTimeout);
    } on TimeoutException {
      throw ApiException('The GAIA server at ${config.backendUrl} did not respond in time.');
    } catch (e) {
      throw ApiException('Cannot reach the GAIA server at ${config.backendUrl}. Check Settings.');
    }
    if (response.statusCode < 200 || response.statusCode >= 300) {
      final text = utf8.decode(await response.stream.toBytes(), allowMalformed: true);
      dynamic data;
      try {
        data = text.isEmpty ? null : jsonDecode(text);
      } on FormatException {
        data = text;
      }
      throw ApiException(_detail(data) ?? 'Request failed (HTTP ${response.statusCode})', statusCode: response.statusCode);
    }
    final parser = SseParser();
    try {
      await for (final text in response.stream.transform(utf8.decoder)) {
        for (final event in parser.add(text)) {
          yield event;
        }
      }
    } on http.ClientException {
      throw const ApiException('The connection to the GAIA server was interrupted.');
    }
  }

  Future<dynamic> delete(String path) => _send(() => _http.delete(uri(path), headers: authHeaders), const Duration(seconds: 20));

  Future<dynamic> postMultipart(
    String path, {
    required String field,
    required List<int> bytes,
    required String filename,
    Map<String, String> fields = const {},
    Map<String, String>? query,
    Duration timeout = const Duration(seconds: 120),
  }) {
    return _send(() async {
      final request = http.MultipartRequest('POST', uri(path, query))
        ..headers.addAll(authHeaders)
        ..fields.addAll(fields)
        ..files.add(http.MultipartFile.fromBytes(field, bytes, filename: filename));
      return http.Response.fromStream(await _http.send(request));
    }, timeout);
  }

  Future<dynamic> _send(Future<http.Response> Function() call, Duration timeout) async {
    final http.Response response;
    try {
      response = await call().timeout(timeout);
    } on TimeoutException {
      throw ApiException('The GAIA server at ${config.backendUrl} did not respond in time.');
    } on http.ClientException {
      throw ApiException('Cannot reach the GAIA server at ${config.backendUrl}. Check Settings.');
    } catch (e) {
      // dart:io SocketException etc. (not referenced directly so the web build stays clean)
      throw ApiException('Cannot reach the GAIA server at ${config.backendUrl} ($e).');
    }
    final text = utf8.decode(response.bodyBytes);
    dynamic data;
    try {
      data = text.isEmpty ? null : jsonDecode(text);
    } on FormatException {
      data = text;
    }
    if (response.statusCode >= 200 && response.statusCode < 300) return data;
    throw ApiException(_detail(data) ?? 'Request failed (HTTP ${response.statusCode})', statusCode: response.statusCode);
  }

  static String? _detail(dynamic data) {
    if (data is Map && data['detail'] != null) {
      final detail = data['detail'];
      if (detail is List) {
        return detail.map((d) => d is Map ? (d['msg'] ?? d.toString()) : d.toString()).join('; ');
      }
      return detail.toString();
    }
    if (data is String && data.isNotEmpty) return data.length > 200 ? data.substring(0, 200) : data;
    return null;
  }
}
