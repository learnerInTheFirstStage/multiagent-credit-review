/**
 * TypeScript shapes that mirror the Pydantic models in the Orchestrator.
 * Keep in sync with services/orchestrator/app/api/cases.py and signals.py.
 */

export type CaseStatus =
  | "queued"
  | "investigating"
  | "waiting_for_data"
  | "needs_approval"
  | "needs_manager"
  | "done"
  | "rejected"
  | "failed";

export type WaitingFor = "analyst" | "manager" | "retry" | null;

export interface Outage {
  service: string;
  codes: string;
  kind: "service" | "data" | "unknown";
  attempts: number;
  since: string;
  next_retry_at: string | null;
  message: string;
}

export type ReviewReason =
  | "large_cut"
  | "failed_repayment"
  | "incomplete_data"
  | "large_limit";

export interface CaseRow {
  case_id: string;
  customer_id: string;
  status: CaseStatus;
  created_at: string;
  updated_at: string;
  outage: Outage | null;
  // Every distinct review_reason the case's signals named, in arrival order.
  signal_reasons: ReviewReason[];
}

export interface CaseList {
  cases: CaseRow[];
  total: number;
}

export interface ReportEvidence {
  claim: string;
  evidence_ids: string[];
}

export interface Report {
  likely_cause?: string;
  summary?: string;
  evidence?: ReportEvidence[];
  ruled_out?: Array<{ cause: string; reason: string; evidence_ids: string[] }>;
  other_findings?: Array<{ claim: string; evidence_ids: string[] }>;
  review_reasons?: Array<{ reason: string; finding?: string }>;
  confidence?: "high" | "medium" | "low";
  recommendation?:
    | "keep_limit"
    | "reduce_limit"
    | "pause_card"
    | "request_documents"
    | "needs_human";
  guideline_ids?: string[];
  next_action?: string;
  [key: string]: unknown;
}

export interface Decision {
  decision: "approve" | "reject";
  note: string | null;
  decided: "keep_limit" | "reduce_limit" | "pause_card" | "request_documents" | null;
  by: string;
  by_role: string;
  decided_at: string;
}

export interface CaseView {
  case_id: string;
  customer_id: string;
  status: CaseStatus;
  waiting_for: WaitingFor;
  report: Report | null;
  decisions: Decision[];
  action: unknown | null;
  outage: Outage | null;
}

export interface CasePathQuestion {
  agent: string;
  topic: string;
  question?: string;
  outcome: string;
  [key: string]: unknown;
}

export interface CasePath {
  case_id: string;
  flow: string | null;
  policy_version: string | null;
  rounds: number;
  refusals: number;
  out_of_rounds: boolean;
  questions: CasePathQuestion[];
  searches: unknown[];
  gaps: unknown[];
  drafts: unknown[];
  history: string[];
  records: Array<{ id: string; kind: string; data: Record<string, unknown> }>;
  handed_over_by: "report" | "low_confidence" | "gave_up" | null;
  outage: Outage | null;
}

export interface User {
  sub: string;
  role: "analyst" | "manager" | "admin" | "service";
  customers: string[];
  // Shown instead of sub when set: an Auth0 sub reads "auth0|6aff…", so the person's email.
  name?: string;
}

export interface AuthState {
  token: string | null;
  user: User | null;
}
