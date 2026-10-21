export type Theme = "light" | "dark" | "system";
export type QueryMode = "question" | "analysis" | "report";
export type ReviewStatus =
  | "draft"
  | "processing"
  | "needs_review"
  | "revision_requested"
  | "approved";

export type Workspace =
  | "home"
  | "analysis"
  | "data"
  | "generator"
  | "report"
  | "project"
  | "templates"
  | "knowledge"
  | "jobs";

export type Role =
  | "Пользователь отчётности"
  | "Аналитик"
  | "Проверяющий"
  | "Методолог"
  | "Владелец шаблонов"
  | "Администратор"
  | "Аудитор";

export interface DataContext {
  form: string;
  territory: string;
  reportingPeriod: string;
  comparisonPeriod: string;
  dataVersion: string;
}

export interface Chat {
  id: string;
  title: string;
  context: string;
  status: ReviewStatus;
  pinned: boolean;
  updatedAt: string;
}

export interface Metric {
  id: string;
  label: string;
  value: string;
  change: string;
  signal: "normal" | "attention" | "adverse" | "unknown";
  sourceId: string;
}

export interface RevenueRow {
  name: string;
  current: number;
  previous: number;
  change: number;
  contribution: number;
  signal: Metric["signal"];
}

export interface SourceReference {
  id: string;
  title: string;
  detail: string;
  version: string;
  loadedAt: string;
  rule: string;
  verified: boolean;
}

export interface ReportTemplate {
  id: string;
  name: string;
  purpose: string;
  owner: string;
  version: string;
  status: "draft" | "review" | "approved";
  updatedAt: string;
  frequent?: boolean;
}

export interface TagDefinition {
  id: string;
  code: string;
  name: string;
  resultType: "Значение" | "Таблица" | "График" | "Список";
  status: "valid" | "draft" | "outdated" | "unknown";
  version: string;
}

export interface ProcessingStep {
  id: number;
  label: string;
  status: "waiting" | "running" | "done" | "error";
}
