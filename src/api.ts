import { processingLabels } from "./data";
import type { ProcessingStep } from "./types";

export interface QueryPlan {
  intent: string;
  catalog_target?:
    | "overview"
    | "periods"
    | "territories"
    | "metrics"
    | "okved"
    | null;
  metric_codes: string[];
  enterprise_groups: string[];
  territories: string[];
  include_top_territory?: boolean;
  include_bottom_territory?: boolean;
  row_codes: number[];
  okved_codes?: string[];
  periods: string[];
  period_modes?: ("cumulative_ytd" | "standalone_quarter")[];
  period_labels?: string[];
  operations: string[];
  group_by: string[];
  analysis_dimension?: "territory" | "industry" | null;
  ranking_criteria?: ("value" | "growth_rate")[];
  outlier_modes?: ("value" | "growth_rate" | "composite")[];
  comparison_basis?: "previous_year_same_slice";
  limit: number | null;
}

export interface ClarificationOption {
  id: string;
  label: string;
  plan_patch?: Partial<QueryPlan>;
}

export interface AnalysisSectionRow {
  entity?: string;
  kind?: string;
  code?: string | number;
  name?: string;
  unit?: string;
  okved?: string | null;
  territory_type?: string;
  row_code?: number;
  industry?: string;
  region?: string;
  current?: string;
  previous?: string;
  difference?: string;
  growth_rate?: string | null;
  median_growth_rate?: string | null;
  weighted_growth_rate?: string | null;
  regions_count?: number;
  minimum_region?: {
    entity: string;
    growth_rate: string;
  };
  maximum_region?: {
    entity: string;
    growth_rate: string;
  };
  score?: string;
  flagged?: boolean;
  low_base?: boolean;
}

export interface AnalysisSection {
  kind: string;
  title: string;
  rows: AnalysisSectionRow[];
  methodology: string;
  threshold?: string;
  threshold_exceeded?: boolean;
}

export interface ResolvedMetric {
  semantic_code: string;
  name_ru: string;
  short_name_ru: string | null;
  display_name: string;
  report_column_number: number | null;
  unit: string;
}

export interface CatalogItem {
  kind: string;
  code: string;
  label: string;
  detail: string;
}

export interface QuerySource {
  fact_id?: number;
  file: string;
  absolute_path?: string;
  sheet?: string;
  cell?: string;
  row_code?: number;
  report_column?: number;
  report_column_name?: string;
  section?: string;
  raw_value?: string;
  unit?: string;
  formula?: string;
  lineage?: QuerySource[];
  read_only?: boolean;
}

export interface QueryFact {
  fact_id: string;
  metric_code: string;
  metric: string;
  value: string;
  unit: string;
  period_end: string;
  section: string;
  enterprise_group: string;
  territory: string;
  row_code: number;
  row_name?: string;
  okved?: string | null;
  source: QuerySource;
}

export interface QueryCalculation {
  operation: string;
  series_key?: string;
  value: string | null;
  unit: string;
  formula: string;
  inputs: string[];
}

export interface QuerySeriesPoint {
  period_end: string;
  label: string;
  value: string;
  fact_id: string;
}

export interface QuerySeries {
  key: string;
  label: string;
  metric_code: string;
  territory: string;
  enterprise_group: string;
  row_code: number;
  unit: string;
  points: QuerySeriesPoint[];
  changes: {
    from_period: string;
    to_period: string;
    difference: string;
    growth_rate: string | null;
  }[];
  overall_change: {
    from_period: string;
    to_period: string;
    difference: string;
    growth_rate: string | null;
  };
  minimum: Omit<QuerySeriesPoint, "fact_id">;
  maximum: Omit<QuerySeriesPoint, "fact_id">;
}

export interface QueryResult {
  query_id: string;
  conversation_id: string;
  question: string;
  planner_mode?: string;
  turn_id?: string;
  sequence?: number;
  status?:
    | "ready"
    | "no_data"
    | "needs_clarification"
    | "failed"
    | "cancelled"
    | "timed_out";
  query_plan: QueryPlan | null;
  resolved_metrics: ResolvedMetric[];
  catalog_items?: CatalogItem[];
  catalog_summary?: Record<string, unknown>;
  facts: QueryFact[];
  calculations: QueryCalculation[];
  series: QuerySeries[];
  analysis_sections?: AnalysisSection[];
  visualization?: {
    type: "line";
    x_axis: string;
    y_axis: string;
    legend: boolean;
    table_alternative: boolean;
  } | null;
  warnings: string[];
  answer: string;
  answer_mode: string;
  numbers_verified: boolean;
  clarification_question?: string | null;
  clarification_options?: (string | ClarificationOption)[] | null;
  research_plan?: ResearchPlan;
  evidence?: EvidenceItem[];
  claims?: EvidenceClaim[];
  hypotheses?: AnalysisHypothesis[];
  charts?: Record<string, unknown>[];
  tables?: Record<string, unknown>[];
  execution?: {
    model_calls: number;
    tool_calls: number;
    duration_ms: number;
    provider?: string;
    model?: string;
    fallback?: boolean;
    retries?: number;
    synthesis_mode?: string;
    tool_journal: Record<string, unknown>[];
    limits: Record<string, number>;
  };
}

export interface ResearchOperation {
  id: string;
  tool: string;
  goal: string;
  arguments: Record<string, unknown>;
  depends_on: string[];
}

export interface ResearchPlan {
  objective: string;
  research_questions: string[];
  initial_filters: Record<string, unknown>;
  expected_analyses: string[];
  operations: ResearchOperation[];
}

export interface EvidenceItem {
  evidence_id: string;
  operation_id: string;
  tool: string;
  title: string;
  periods: string[];
  unit: string | null;
  methodology: string;
  sources: string[];
  rows: Record<string, unknown>[];
  verified: boolean;
}

export interface EvidenceClaim {
  claim_id: string;
  text: string;
  evidence_ids: string[];
  numbers: string[];
  periods: string[];
  entities: string[];
}

export interface AnalysisHypothesis {
  hypothesis_id: string;
  label: "Гипотеза";
  text: string;
  evidence_ids: string[];
  status: "possible_interpretation";
}

export interface AnalysisRun {
  run_id: string;
  turn_id: string;
  conversation_id?: string;
  status:
    | "queued"
    | "processing"
    | "needs_clarification"
    | "ready"
    | "no_data"
    | "failed"
    | "cancelled"
    | "timed_out";
  stage?: string;
  progress?: number;
  research_plan?: ResearchPlan | null;
  tool_journal?: Record<string, unknown>[];
  clarification?: {
    question: string;
    options: ClarificationOption[];
  } | null;
  result?: QueryResult | null;
  error?: { code: string; message: string } | null;
  events_url: string;
}

export interface QueryPlanResponse {
  question: string;
  conversation_id: string;
  status: "ready" | "no_data" | "needs_clarification";
  plan: QueryPlan | null;
  resolved_metrics: ResolvedMetric[];
  planner_mode: string;
  warnings: string[];
  requires_confirmation: boolean;
  clarification_question?: string | null;
  clarification_options?: string[] | null;
}

export interface ConversationSummary {
  id: string;
  title: string;
  status: string;
  pinned: boolean;
  archived: boolean;
  context: string;
  created_at: string;
  updated_at: string;
  project_id: string | null;
  turns_count: number;
}

export interface ConversationTurn {
  id: string;
  sequence: number;
  question: string;
  status:
    | "ready"
    | "no_data"
    | "needs_clarification"
    | "failed"
    | "processing";
  turn_type: string;
  plan: QueryPlan | ResearchPlan | null;
  result: QueryResult | null;
  clarification: {
    question: string;
    options: ClarificationOption[];
  } | null;
  created_at: string;
}

export interface ConversationDetail extends ConversationSummary {
  state: Record<string, unknown>;
  turns: ConversationTurn[];
  project: AnalysisProject | null;
}

export interface AnalysisProject {
  id: string;
  conversation_id: string;
  title: string;
  version: number;
  blocks_count: number;
  blocks?: {
    id: string;
    turn_id: string;
    position: number;
    question: string;
  }[];
  document: ReportDocument;
  download_url: string | null;
  created_at: string;
  updated_at: string;
}

export interface AnalysisSynthesisResult {
  project: AnalysisProject;
  synthesis: {
    title: string;
    subtitle: string;
    summary: string;
    findings: string[];
    caveats: string[];
    section_order: string[];
    sections: {
      block_id: string;
      heading: string;
      narrative: string;
    }[];
  };
  mode: string;
  status: "success" | "fallback";
  used_ai: boolean;
  provider: string;
  model: string;
  error_code: string | null;
  message: string;
  numbers_verified: boolean;
}

export interface Scenario {
  id: string;
  title: string;
  source_conversation_id: string | null;
  steps: {
    question: string;
    plan_template: QueryPlan;
    period_strategy: string;
  }[];
  created_at: string;
  updated_at: string;
}

export interface ReportResult {
  id: number;
  status: string;
  old_period_end: string;
  new_period_end: string;
  download_url: string | null;
  facts_url: string | null;
  document_url: string | null;
  numbers_verified: boolean;
}

export type ReportMode = "national" | "region_vs_rf";

export interface ReportGenerateParameters {
  report_mode: ReportMode;
  territory: string | null;
  old_period_end: string | null;
  new_period_end: string | null;
  old_year?: number;
  new_year?: number;
  months_count?: number;
}

export interface ReportPeriodOption {
  period_end: string;
  label: string;
  series_kind?: "cumulative_ytd";
  months_count?: number;
}

export interface ReportPeriodPair {
  old_period_end: string;
  new_period_end: string;
  old_label: string;
  new_label: string;
}

export interface ReportParameterCatalog {
  report_modes: {
    code: ReportMode;
    name: string;
    benchmark: string | null;
  }[];
  territories: { name: string; aliases: string[] }[];
  periods: ReportPeriodOption[];
  comparable_pairs: ReportPeriodPair[];
  recommended_pair: ReportPeriodPair | null;
  national_legacy_pair: ReportPeriodPair;
  required_benchmark: "Российская Федерация";
}

export interface ReportSpecification {
  status: "ready" | "clarification" | "no_data";
  report_mode: ReportMode | null;
  territory: string | null;
  benchmark: "Российская Федерация" | null;
  old_period_end: string | null;
  new_period_end: string | null;
  clarification_question: string | null;
  no_data_reason: string | null;
}

export interface ReportParagraphBlock {
  id: string;
  type: "paragraph";
  text: string;
  style: string | null;
  editable: boolean;
}

export interface ReportTableBlock {
  id: string;
  type: "table";
  rows: string[][];
  editable: false;
}

export interface ReportChartBlock {
  id: string;
  type: "chart";
  title: string;
  context: string;
  chart_type?: "line" | "bar";
  period_context?: string;
  mode: "change" | "absolute";
  unit: string;
  series: {
    key: string;
    label: string;
    unit: string;
    points: {
      period_end: string;
      label: string;
      axis_label?: string;
      value: string;
    }[];
  }[];
  editable: false;
}

export interface ReportDocument {
  version: number;
  presentation_version?: number;
  synthesis_mode?: string;
  blocks: (ReportParagraphBlock | ReportTableBlock | ReportChartBlock)[];
  paragraph_count: number;
  table_count: number;
  chart_count?: number;
}

export interface AiStatus {
  status: string;
  provider: string;
  model: string;
  narrative_model: string;
  data_policy: string;
  external_data_allowed: boolean;
  narrative_improvement_enabled: boolean;
  report_synthesis_provider: string;
  report_synthesis_model: string;
  report_synthesis_status: "configured" | "not_configured";
  fallback?: boolean;
}

export interface DataScanItem {
  path: string;
  name: string;
  format: string;
  size_bytes: number;
  workbook_kind: string;
  as_of_date: string | null;
  numeric_columns: number | null;
  status: "ready" | "quarantine" | "ignored";
  warnings: string[];
}

export interface DataScanResult {
  root: string;
  files_found: number;
  ready: number;
  quarantine: number;
  ignored: number;
  items: DataScanItem[];
}

export interface ImportResult {
  id: number;
  status: string;
  files_found: number;
  files_imported: number;
  files_quarantined: number;
  errors_count: number;
  finished_at: string | null;
}

export interface ApiClient {
  streamQuery(
    query: string,
    onChunk: (text: string) => void,
    signal: AbortSignal,
    plan?: QueryPlan,
    conversationId?: string,
  ): Promise<QueryResult | void>;
  generateReport(
    onProgress: (steps: ProcessingStep[]) => void,
    signal: AbortSignal,
    parameters?: ReportGenerateParameters,
  ): Promise<ReportResult | void>;
}

const wait = (milliseconds: number, signal: AbortSignal) =>
  new Promise<void>((resolve, reject) => {
    const timer = window.setTimeout(resolve, milliseconds);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Операция остановлена", "AbortError"));
      },
      { once: true },
    );
  });

async function requestJson<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init.headers,
    },
  });
  if (!response.ok) {
    let message = `HTTP ${response.status}`;
    try {
      const body = (await response.json()) as { detail?: string };
      message = body.detail || message;
    } catch {
      // Keep the HTTP status when the body is not JSON.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

export class HttpApiClient implements ApiClient {
  async streamQuery(
    query: string,
    onChunk: (text: string) => void,
    signal: AbortSignal,
    plan?: QueryPlan,
    conversationId?: string,
  ) {
    const result = await requestJson<QueryResult>("/api/query", {
      method: "POST",
      body: JSON.stringify({
        question: query,
        plan: plan || null,
        conversation_id: conversationId || null,
      }),
      signal,
    });
    const chunks = result.answer.match(/.{1,48}(?:\s|$)/gu) || [result.answer];
    for (const chunk of chunks) {
      if (signal.aborted) {
        throw new DOMException("Операция остановлена", "AbortError");
      }
      onChunk(chunk);
      await wait(18, signal);
    }
    return result;
  }

  async generateReport(
    onProgress: (steps: ProcessingStep[]) => void,
    signal: AbortSignal,
    parameters?: ReportGenerateParameters,
  ) {
    const steps: ProcessingStep[] = processingLabels.map((label, index) => ({
      id: index,
      label,
      status: "waiting",
    }));
    steps[0].status = "running";
    onProgress([...steps]);

    const request = requestJson<ReportResult>("/api/reports/1-msp/generate", {
      method: "POST",
      body: JSON.stringify(
        parameters || {
          old_year: 2025,
          new_year: 2026,
          months_count: 6,
        },
      ),
      signal,
    });

    for (let index = 1; index < steps.length - 1; index += 1) {
      await wait(260, signal);
      steps[index - 1].status = "done";
      steps[index].status = "running";
      onProgress([...steps]);
    }
    const result = await request;
    steps.forEach((step) => {
      step.status = "done";
    });
    onProgress([...steps]);
    return result;
  }
}

export class MockApiClient implements ApiClient {
  async streamQuery(
    _query: string,
    onChunk: (text: string) => void,
    signal: AbortSignal,
    _plan?: QueryPlan,
    _conversationId?: string,
  ) {
    const chunks = [
      "Снижение совокупных поступлений связано прежде всего ",
      "с налогом на прибыль организаций. Его поступления уменьшились ",
      "на 3,3 млрд ₽, что сформировало основную часть отрицательного отклонения. ",
      "Наибольший вклад внесли обрабатывающие производства. ",
      "Данные по двум источникам требуют ручной проверки перед утверждением.",
    ];

    for (const chunk of chunks) {
      await wait(210, signal);
      onChunk(chunk);
    }
  }

  async generateReport(
    onProgress: (steps: ProcessingStep[]) => void,
    signal: AbortSignal,
    _parameters?: ReportGenerateParameters,
  ) {
    const steps: ProcessingStep[] = processingLabels.map((label, index) => ({
      id: index,
      label,
      status: "waiting",
    }));
    onProgress(steps);

    for (let index = 0; index < steps.length; index += 1) {
      steps[index].status = "running";
      onProgress([...steps]);
      await wait(index === 5 ? 620 : 360, signal);
      steps[index].status = "done";
      onProgress([...steps]);
    }
  }
}

export const DEMO_MODE = import.meta.env.VITE_DEMO_MODE === "true";
export const ANALYTIC_AGENT_V2 =
  import.meta.env.VITE_ANALYTIC_AGENT_V2 === "true";
export const apiClient: ApiClient = DEMO_MODE
  ? new MockApiClient()
  : new HttpApiClient();

export const backendApi = {
  reportParameters: (signal?: AbortSignal) =>
    requestJson<ReportParameterCatalog>(
      "/api/reports/1-msp/parameters",
      { signal },
    ),
  interpretReportCommand: (command: string, signal?: AbortSignal) =>
    requestJson<ReportSpecification>("/api/reports/1-msp/interpret", {
      method: "POST",
      body: JSON.stringify({ command }),
      signal,
    }),
  conversations: (search = "", signal?: AbortSignal) =>
    requestJson<ConversationSummary[]>(
      `/api/conversations?search=${encodeURIComponent(search)}`,
      { signal },
    ),
  createConversation: (title?: string, signal?: AbortSignal) =>
    requestJson<ConversationDetail>("/api/conversations", {
      method: "POST",
      body: JSON.stringify({ title: title || null }),
      signal,
    }),
  conversation: (conversationId: string, signal?: AbortSignal) =>
    requestJson<ConversationDetail>(
      `/api/conversations/${conversationId}`,
      { signal },
    ),
  updateConversation: (
    conversationId: string,
    patch: Partial<{
      title: string;
      pinned: boolean;
      archived: boolean;
      status: string;
    }>,
    signal?: AbortSignal,
  ) =>
    requestJson<ConversationDetail>(
      `/api/conversations/${conversationId}`,
      {
        method: "PATCH",
        body: JSON.stringify(patch),
        signal,
      },
    ),
  archiveConversation: (conversationId: string, signal?: AbortSignal) =>
    requestJson<{ id: string; archived: boolean }>(
      `/api/conversations/${conversationId}`,
      { method: "DELETE", signal },
    ),
  deleteConversation: (conversationId: string, signal?: AbortSignal) =>
    requestJson<{ id: string; deleted: boolean }>(
      `/api/conversations/${conversationId}/permanent`,
      { method: "DELETE", signal },
    ),
  createConversationTurn: (
    conversationId: string,
    question: string,
    signal?: AbortSignal,
  ) =>
    requestJson<QueryResult>(
      `/api/conversations/${conversationId}/turns`,
      {
        method: "POST",
        body: JSON.stringify({ question }),
        signal,
      },
    ),
  createAnalysisRun: (
    conversationId: string,
    question: string,
    signal?: AbortSignal,
  ) =>
    requestJson<AnalysisRun>(
      `/api/conversations/${conversationId}/analysis-runs`,
      {
        method: "POST",
        body: JSON.stringify({ question }),
        signal,
      },
    ),
  analysisRun: (runId: string, signal?: AbortSignal) =>
    requestJson<AnalysisRun>(`/api/analysis-runs/${runId}`, { signal }),
  cancelAnalysisRun: (runId: string, signal?: AbortSignal) =>
    requestJson<AnalysisRun | { run_id: string; status: string }>(
      `/api/analysis-runs/${runId}/cancel`,
      { method: "POST", body: "{}", signal },
    ),
  clarifyAnalysisRun: (
    runId: string,
    answer: { option_id?: string; text?: string },
    signal?: AbortSignal,
  ) =>
    requestJson<AnalysisRun>(
      `/api/analysis-runs/${runId}/clarification`,
      {
        method: "POST",
        body: JSON.stringify(answer),
        signal,
      },
    ),
  waitForAnalysisRun: async (
    initial: AnalysisRun,
    onStage: (stage: string, progress: number) => void,
    signal?: AbortSignal,
  ): Promise<AnalysisRun> => {
    const response = await fetch(initial.events_url, {
      headers: { Accept: "text/event-stream" },
      signal,
    });
    if (!response.ok || !response.body) {
      throw new Error(`HTTP ${response.status}`);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() || "";
      for (const frame of frames) {
        const dataLine = frame
          .split("\n")
          .find((line) => line.startsWith("data: "));
        if (!dataLine) continue;
        const event = JSON.parse(dataLine.slice(6)) as {
          stage?: string;
          progress?: number;
        };
        onStage(event.stage || "анализ", event.progress || 0);
      }
    }
    return requestJson<AnalysisRun>(
      `/api/analysis-runs/${initial.run_id}`,
      { signal },
    );
  },
  answerClarification: (
    conversationId: string,
    turnId: string,
    answer: { option_id?: string; text?: string },
    signal?: AbortSignal,
  ) =>
    requestJson<QueryResult>(
      `/api/conversations/${conversationId}/turns/${turnId}/clarification`,
      {
        method: "POST",
        body: JSON.stringify(answer),
        signal,
      },
    ),
  conversationProject: (
    conversationId: string,
    signal?: AbortSignal,
  ) =>
    requestJson<AnalysisProject>(
      `/api/conversations/${conversationId}/project`,
      { signal },
    ),
  addProjectBlock: (
    conversationId: string,
    turnId: string,
    signal?: AbortSignal,
  ) =>
    requestJson<{ block_id: string; added: boolean; project: AnalysisProject }>(
      `/api/conversations/${conversationId}/project/blocks`,
      {
        method: "POST",
        body: JSON.stringify({ turn_id: turnId }),
        signal,
      },
    ),
  removeProjectBlock: (
    conversationId: string,
    blockId: string,
    signal?: AbortSignal,
  ) =>
    requestJson<AnalysisProject>(
      `/api/conversations/${conversationId}/project/blocks/${blockId}`,
      { method: "DELETE", signal },
    ),
  updateAnalysisProject: (
    projectId: string,
    baseVersion: number,
    changedTextBlocks: Record<string, string>,
    orderedBlockIds?: string[],
    signal?: AbortSignal,
  ) =>
    requestJson<AnalysisProject>(`/api/analysis-projects/${projectId}`, {
      method: "PATCH",
      body: JSON.stringify({
        base_version: baseVersion,
        changed_text_blocks: changedTextBlocks,
        ordered_block_ids: orderedBlockIds,
      }),
      signal,
    }),
  synthesizeAnalysisProject: (
    projectId: string,
    signal?: AbortSignal,
  ) =>
    requestJson<AnalysisSynthesisResult>(
      `/api/analysis-projects/${projectId}/synthesize`,
      {
        method: "POST",
        body: "{}",
        signal,
      },
    ),
  saveScenario: (
    conversationId: string,
    title?: string,
    signal?: AbortSignal,
  ) =>
    requestJson<Scenario>("/api/scenarios", {
      method: "POST",
      body: JSON.stringify({
        conversation_id: conversationId,
        title: title || null,
      }),
      signal,
    }),
  scenarios: (signal?: AbortSignal) =>
    requestJson<Scenario[]>("/api/scenarios", { signal }),
  runScenario: (
    scenarioId: string,
    parameters: Record<string, string> = {},
    signal?: AbortSignal,
  ) =>
    requestJson<{
      scenario_id: string;
      conversation: ConversationDetail;
      results: QueryResult[];
    }>(`/api/scenarios/${scenarioId}/run`, {
      method: "POST",
      body: JSON.stringify({ parameters }),
      signal,
    }),
  planQuery: (
    question: string,
    conversationId?: string,
    signal?: AbortSignal,
  ) =>
    requestJson<QueryPlanResponse>("/api/query/plan", {
      method: "POST",
      body: JSON.stringify({
        question,
        conversation_id: conversationId || null,
      }),
      signal,
    }),
  aiStatus: (signal?: AbortSignal) =>
    requestJson<AiStatus>("/api/ai/status", { signal }),
  scanData: (signal?: AbortSignal) =>
    requestJson<DataScanResult>("/api/data/scan", {
      method: "POST",
      signal,
    }),
  importData: (paths?: string[], signal?: AbortSignal) =>
    requestJson<ImportResult>("/api/data/import", {
      method: "POST",
      body: JSON.stringify({ paths: paths || null }),
      signal,
    }),
  periods: (signal?: AbortSignal) =>
    requestJson<
      {
        period_end: string;
        as_of_date: string | null;
        facts_count: number;
        status: string;
      }[]
    >("/api/catalog/periods", { signal }),
  reportAction: (
    reportId: number,
    action: "submit" | "approve" | "request-revision",
    signal?: AbortSignal,
  ) =>
    requestJson<ReportResult>(`/api/reports/${reportId}/${action}`, {
      method: "POST",
      body: "{}",
      signal,
    }),
  reportDocument: (reportId: number, signal?: AbortSignal) =>
    requestJson<ReportDocument>(`/api/reports/${reportId}/document`, {
      signal,
    }),
  updateReportDocument: (
    reportId: number,
    baseVersion: number,
    changedTextBlocks: Record<string, string>,
    signal?: AbortSignal,
  ) =>
    requestJson<ReportDocument>(`/api/reports/${reportId}/document`, {
      method: "PATCH",
      body: JSON.stringify({
        base_version: baseVersion,
        changed_text_blocks: changedTextBlocks,
      }),
      signal,
    }),
};
