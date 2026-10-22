import 'package:flutter_test/flutter_test.dart';
import 'package:gaia_farm_assistant/models/ai_models.dart';
import 'package:gaia_farm_assistant/models/rover_models.dart';
import 'package:gaia_farm_assistant/models/sensor_reading.dart';

void main() {
  test('parses a chat reply with sources and grounding', () {
    final reply = ChatReply.fromJson({
      'session_id': 'abc',
      'answer': 'Soil averaged 40.5% today [F1], lower than yesterday [F3].',
      'sources': [
        {'id': 'F1', 'kind': 'sensor_fact', 'title': 'Soil moisture · today', 'snippet': 'average 40.5%', 'time_start': '2025-06-15T00:00:00+05:30'},
        {'id': 'W1', 'kind': 'web', 'title': 'Early blight', 'snippet': '...', 'url': 'https://extension.umn.edu/x', 'domain': 'extension.umn.edu', 'quality': 'institutional'},
      ],
      'grounding': {'farm_data': true, 'knowledge_store': false, 'general_knowledge': false, 'web': true},
      'follow_ups': ['What about last week?'],
      'llm_used': true,
      'notices': [],
      'plan': {'intent': 'comparison'},
    });
    expect(reply.sessionId, 'abc');
    expect(reply.message.sources.map((s) => s.id), ['F1', 'W1']);
    expect(reply.message.sources.first.kindLabel, 'Your sensor data');
    expect(reply.message.grounding.farmData, isTrue);
    expect(reply.message.grounding.knowledgeStore, isFalse);
    expect(reply.message.followUps, hasLength(1));
    expect(reply.plan['intent'], 'comparison');
  });

  test('parses a completed diagnosis and exposes findings sections', () {
    final d = DiagnosisResult.fromJson({
      'id': 'd1',
      'status': 'completed',
      'stage': 'done',
      'image_url': '/api/v1/diagnoses/d1/image',
      'created_at': '2025-06-15T10:00:00+00:00',
      'analysis': {
        'is_plant_image': true,
        'plant': 'Tomato',
        'health_status': 'unhealthy',
        'diagnosis': 'Possible early blight',
        'issue_categories': ['fungal_infection'],
        'evidence': ['Concentric brown lesions'],
        'confidence': 0.78,
        'image_quality': 'good',
      },
      'findings': {
        'headline': 'Act early [W1].',
        'recommended_actions': ['Remove infected leaves [W1]'],
        'preventive_measures': ['Mulch'],
        'when_to_consult_expert': 'If spreading fast.',
      },
      'sources': [
        {'id': 'W1', 'title': 'Early blight', 'url': 'https://extension.umn.edu/x', 'domain': 'extension.umn.edu', 'quality': 'institutional', 'cited': true},
      ],
      'web_grounded': true,
      'confidence_note': 'not a calibrated probability',
      'recapture_tips': [],
      'agent_trace': [
        {'round': 1, 'queries': ['tomato early blight treatment']},
      ],
      'knowledge_chunk_ids': ['a', 'b'],
    });
    expect(d.isProcessing, isFalse);
    expect(d.analysis!.status, HealthStatus.unhealthy);
    expect(d.analysis!.confidence, closeTo(0.78, 1e-9));
    expect(d.findings!.sections.map((s) => s.$1), ['Recommended actions', 'Prevention', 'When to consult an expert']);
    expect(d.sources.single.cited, isTrue);
    expect(d.agentQueries, ['tomato early blight treatment']);
    expect(d.knowledgeEntries, 2);
  });

  test('parses sensor readings and rover status', () {
    final r = SensorReading.fromJson({
      'sensor': 'mq5', 'label': 'MQ5 gas index', 'unit': 'ADC', 'value': 469, 'display': '469 ADC',
      'ts': '2025-06-15T10:00:00+00:00', 'band': 'elevated', 'progress': 0.46, 'rag': true,
    });
    expect(r.kind, SensorKind.airQuality);
    expect(r.isAlert, isTrue);

    final s = RoverStatus.fromJson({
      'device_id': 'rover-01', 'firmware': 'gaia-esp32cam-1.0', 'uptime_ms': 120000,
      'wifi': {'mode': 'sta', 'ssid': 'Farm', 'rssi': -61, 'ip': '192.168.1.57'},
      'nano': {'connected': true}, 'camera': {'ok': true}, 'drive': {'left': 0, 'right': 0},
      'arm': 'deployed', 'obstacle_cm': 84, 'battery_v': 7.86,
      'backend': {'last_push_ok': true, 'queued': 0},
    });
    expect(s.armDeployed, isTrue);
    expect(s.moving, isFalse);
    expect(s.uptime.inMinutes, 2);
    expect(s.backendPushOk, isTrue);
  });
}
