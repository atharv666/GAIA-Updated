import 'json_utils.dart';

// ---------------------------------------------------------------------------
// Pipeline A - chat
// ---------------------------------------------------------------------------

enum ChatRole { user, gaia }

enum ChatMode { farm, open }

/// A retrieved context item the answer can cite: [F1] sensor fact, [K2] knowledge, [W3] web.
class SourceRef {
  const SourceRef({
    required this.id,
    required this.kind,
    required this.title,
    required this.snippet,
    this.sourceType,
    this.url,
    this.domain,
    this.quality,
    this.timeStart,
  });

  factory SourceRef.fromJson(Map<String, dynamic> j) => SourceRef(
        id: str(j['id']),
        kind: str(j['kind']),
        title: str(j['title']),
        snippet: str(j['snippet']),
        sourceType: strOrNull(j['source_type']),
        url: strOrNull(j['url']),
        domain: strOrNull(j['domain']),
        quality: strOrNull(j['quality']),
        timeStart: time(j['time_start']),
      );

  final String id;
  final String kind; // sensor_fact | knowledge | web
  final String title;
  final String snippet;
  final String? sourceType;
  final String? url;
  final String? domain;
  final String? quality;
  final DateTime? timeStart;

  String get kindLabel => switch (kind) {
        'sensor_fact' => 'Your sensor data',
        'knowledge' => 'Knowledge store',
        'web' => 'Web source',
        _ => kind,
      };
}

class Grounding {
  const Grounding({
    this.farmData = false,
    this.knowledgeStore = false,
    this.web = false,
    this.generalKnowledge = false,
    this.insufficientData = false,
  });

  factory Grounding.fromJson(Map<String, dynamic> j) => Grounding(
        farmData: boolean(j['farm_data']),
        knowledgeStore: boolean(j['knowledge_store']),
        web: boolean(j['web']),
        generalKnowledge: boolean(j['general_knowledge']),
        insufficientData: boolean(j['insufficient_data']),
      );

  final bool farmData;
  final bool knowledgeStore;
  final bool web;
  final bool generalKnowledge;
  final bool insufficientData;
}

class ChatMessage {
  const ChatMessage({
    required this.role,
    required this.text,
    this.sources = const [],
    this.grounding = const Grounding(),
    this.followUps = const [],
    this.notices = const [],
    this.llmUsed = true,
    this.isError = false,
  });

  final ChatRole role;
  final String text;
  final List<SourceRef> sources;
  final Grounding grounding;
  final List<String> followUps;
  final List<String> notices;
  final bool llmUsed;
  final bool isError;
}

class ChatReply {
  const ChatReply({required this.sessionId, required this.message, required this.plan});

  factory ChatReply.fromJson(Map<String, dynamic> j) => ChatReply(
        sessionId: str(j['session_id']),
        plan: map(j['plan']),
        message: ChatMessage(
          role: ChatRole.gaia,
          text: str(j['answer']),
          sources: maps(j['sources']).map(SourceRef.fromJson).toList(),
          grounding: Grounding.fromJson(map(j['grounding'])),
          followUps: strings(j['follow_ups']),
          notices: strings(j['notices']),
          llmUsed: boolean(j['llm_used'], true),
        ),
      );

  final String sessionId;
  final ChatMessage message;
  final Map<String, dynamic> plan;
}

// ---------------------------------------------------------------------------
// Pipeline B - diagnosis
// ---------------------------------------------------------------------------

enum HealthStatus { healthy, unhealthy, uncertain }

HealthStatus healthFrom(String? v) => switch (v) {
      'healthy' => HealthStatus.healthy,
      'unhealthy' => HealthStatus.unhealthy,
      _ => HealthStatus.uncertain,
    };

/// Structured output of the vision-language model.
class VlmAnalysis {
  const VlmAnalysis({
    required this.isPlantImage,
    required this.plant,
    required this.status,
    required this.diagnosis,
    required this.evidence,
    required this.confidence,
    this.scientificName,
    this.plantPart,
    this.issueCategories = const [],
    this.assumptions = const [],
    this.differential = const [],
    this.severity = 'unknown',
    this.imageQuality = 'fair',
    this.imageQualityNotes,
  });

  factory VlmAnalysis.fromJson(Map<String, dynamic> j) => VlmAnalysis(
        isPlantImage: boolean(j['is_plant_image'], true),
        plant: str(j['plant'], 'Unknown'),
        scientificName: strOrNull(j['plant_scientific_name']),
        plantPart: strOrNull(j['plant_part']),
        status: healthFrom(strOrNull(j['health_status'])),
        diagnosis: str(j['diagnosis']),
        issueCategories: strings(j['issue_categories']),
        evidence: strings(j['evidence']),
        assumptions: strings(j['assumptions']),
        differential: strings(j['differential']),
        severity: str(j['severity'], 'unknown'),
        confidence: dbl(j['confidence']),
        imageQuality: str(j['image_quality'], 'fair'),
        imageQualityNotes: strOrNull(j['image_quality_notes']),
      );

  final bool isPlantImage;
  final String plant;
  final String? scientificName;
  final String? plantPart;
  final HealthStatus status;
  final String diagnosis;
  final List<String> issueCategories;
  final List<String> evidence;
  final List<String> assumptions;
  final List<String> differential;
  final String severity;
  final double confidence;
  final String imageQuality;
  final String? imageQualityNotes;
}

/// What the single research agent wrote after searching the web.
class AgentFindings {
  const AgentFindings({
    required this.headline,
    this.conditionOverview = '',
    this.symptomsToCheck = const [],
    this.possibleCauses = const [],
    this.recommendedActions = const [],
    this.preventiveMeasures = const [],
    this.careGuidance = const [],
    this.distinguishingChecks = const [],
    this.whenToConsultExpert = '',
  });

  factory AgentFindings.fromJson(Map<String, dynamic> j) => AgentFindings(
        headline: str(j['headline']),
        conditionOverview: str(j['condition_overview']),
        symptomsToCheck: strings(j['symptoms_to_check']),
        possibleCauses: strings(j['possible_causes']),
        recommendedActions: strings(j['recommended_actions']),
        preventiveMeasures: strings(j['preventive_measures']),
        careGuidance: strings(j['care_guidance']),
        distinguishingChecks: strings(j['distinguishing_checks']),
        whenToConsultExpert: str(j['when_to_consult_expert']),
      );

  final String headline;
  final String conditionOverview;
  final List<String> symptomsToCheck;
  final List<String> possibleCauses;
  final List<String> recommendedActions;
  final List<String> preventiveMeasures;
  final List<String> careGuidance;
  final List<String> distinguishingChecks;
  final String whenToConsultExpert;

  /// Non-empty sections in presentation order.
  List<(String, List<String>)> get sections => [
        if (conditionOverview.isNotEmpty) ('About the condition', [conditionOverview]),
        if (symptomsToCheck.isNotEmpty) ('Symptoms to check', symptomsToCheck),
        if (possibleCauses.isNotEmpty) ('Possible causes', possibleCauses),
        if (recommendedActions.isNotEmpty) ('Recommended actions', recommendedActions),
        if (preventiveMeasures.isNotEmpty) ('Prevention', preventiveMeasures),
        if (careGuidance.isNotEmpty) ('Care guidance', careGuidance),
        if (distinguishingChecks.isNotEmpty) ('How to confirm', distinguishingChecks),
        if (whenToConsultExpert.isNotEmpty) ('When to consult an expert', [whenToConsultExpert]),
      ];
}

class WebSource {
  const WebSource({required this.id, required this.title, required this.url, required this.domain, this.quality = 'general', this.cited = false});

  factory WebSource.fromJson(Map<String, dynamic> j) => WebSource(
        id: str(j['id']),
        title: str(j['title']),
        url: str(j['url']),
        domain: str(j['domain']),
        quality: str(j['quality'], 'general'),
        cited: boolean(j['cited']),
      );

  final String id;
  final String title;
  final String url;
  final String domain;
  final String quality;
  final bool cited;
}

class DiagnosisResult {
  const DiagnosisResult({
    required this.id,
    required this.status,
    required this.stage,
    required this.imageUrl,
    required this.createdAt,
    this.source = 'phone',
    this.note,
    this.error,
    this.analysis,
    this.findings,
    this.sources = const [],
    this.webGrounded = false,
    this.confidenceNote = '',
    this.disclaimer = '',
    this.recaptureTips = const [],
    this.agentQueries = const [],
    this.knowledgeEntries = 0,
  });

  factory DiagnosisResult.fromJson(Map<String, dynamic> j) => DiagnosisResult(
        id: str(j['id']),
        status: str(j['status'], 'processing'),
        stage: str(j['stage'], 'queued'),
        imageUrl: str(j['image_url']),
        createdAt: time(j['created_at']) ?? DateTime.now(),
        source: str(j['source'], 'phone'),
        note: strOrNull(j['note']),
        error: strOrNull(j['error']),
        analysis: j['analysis'] is Map ? VlmAnalysis.fromJson(map(j['analysis'])) : null,
        findings: j['findings'] is Map ? AgentFindings.fromJson(map(j['findings'])) : null,
        sources: maps(j['sources']).map(WebSource.fromJson).toList(),
        webGrounded: boolean(j['web_grounded']),
        confidenceNote: str(j['confidence_note']),
        disclaimer: str(j['disclaimer']),
        recaptureTips: strings(j['recapture_tips']),
        agentQueries: maps(j['agent_trace']).expand((t) => strings(t['queries'])).toList(),
        knowledgeEntries: (j['knowledge_chunk_ids'] as List?)?.length ?? 0,
      );

  final String id;
  final String status; // processing | completed | not_plant | failed
  final String stage; // queued | vlm | searching | writing | done
  final String imageUrl;
  final DateTime createdAt;
  final String source;
  final String? note;
  final String? error;
  final VlmAnalysis? analysis;
  final AgentFindings? findings;
  final List<WebSource> sources;
  final bool webGrounded;
  final String confidenceNote;
  final String disclaimer;
  final List<String> recaptureTips;
  final List<String> agentQueries;
  final int knowledgeEntries;

  bool get isProcessing => status == 'processing';
}

/// Row in the diagnosis history list.
class DiagnosisSummary {
  const DiagnosisSummary({
    required this.id,
    required this.status,
    required this.imageUrl,
    required this.createdAt,
    this.plant,
    this.health,
    this.diagnosis,
    this.confidence,
  });

  factory DiagnosisSummary.fromJson(Map<String, dynamic> j) => DiagnosisSummary(
        id: str(j['id']),
        status: str(j['status']),
        imageUrl: str(j['image_url']),
        createdAt: time(j['created_at']) ?? DateTime.now(),
        plant: strOrNull(j['plant']),
        health: j['health_status'] == null ? null : healthFrom(strOrNull(j['health_status'])),
        diagnosis: strOrNull(j['diagnosis']),
        confidence: dblOrNull(j['confidence']),
      );

  final String id;
  final String status;
  final String imageUrl;
  final DateTime createdAt;
  final String? plant;
  final HealthStatus? health;
  final String? diagnosis;
  final double? confidence;
}
