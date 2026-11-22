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
  // Open: request_documents was approved and a person records what the customer's documents showed.
  | "awaiting_documents"
  | "done"
  | "rejected"
  | "failed";

export type WaitingFor = "analyst" | "manager" | "documents" | "retry" | null;

export interface Outage {
  service: string;
  codes: string;
  // data: the service does not know the customer, so asking again gets the same answer until that is fixed.
  kind: "transient" | "configuration" | "data";
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
  // Missing from a backend that does not send them yet. recommendation and confidence are the checked report's.
  recommendation?: string | null;
  confidence?: string | null;
  // The engine's cut as basis points of the current limit, from the signal.
  cut_share_bp?: number | null;
  waiting_for?: WaitingFor;
  // A review imported from earlier records (a seed): no signal, and never investigated in this system.
  imported?: boolean;
  // YYYY-MM-DD the customer's documents are due, while the case waits for them.
  documents_due?: string | null;
}

// What a review imported from earlier records holds, as the record kept it.
export interface PastReview {
  as_of: string;
  outcome: string;
  review_reasons: ReviewReason[];
  current_limit_cents: number;
  engine_limit_cents: number;
  cash_7d_ago_cents: number;
  cash_now_cents: number;
  likely_cause: string | null;
  recommendation: string | null;
  confidence: string | null;
  human_decision: string | null;
  guideline_ids: string[];
  claims: string[];
  decisions: Decision[];
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
  other_findings?: ReportEvidence[];
  // What the report found for each reason the engine sent the case. evidence_ids is empty when the
  // specialists could not determine it.
  review_reasons?: Array<{ reason: ReviewReason; finding?: string; evidence_ids?: string[] }>;
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

// Why a person decided: "agree" on an approval, what was wrong on a rejection. Kept with the case's review.
export type DecisionReason =
  | "agree"
  | "wrong_cause"
  | "wrong_action"
  | "missing_evidence"
  | "data_problem"
  | "other";

export interface Decision {
  // "retry": a manager closed a case whose approved action Card never confirmed.
  step?: "analyst" | "manager" | "documents" | "retry";
  decision: "approve" | "reject";
  note?: string | null;
  decided?: "keep_limit" | "reduce_limit" | "pause_card" | "request_documents" | null;
  // Missing from a backend that does not keep it yet, and from Card retry decisions.
  reason?: DecisionReason | null;
  by: string;
  // Kept only when the API stamped them.
  by_role?: string;
  // The person's email, from their token: shown in place of by, which stays who decided.
  by_name?: string;
  decided_at?: string;
}

// A guideline the report cites, with its wording, from the guidelines the case ran on.
export interface Guideline {
  id: string;
  title: string;
  when: string;
  allows: string[];
  note: string | null;
}

// The one change Card can be asked to make: reduce_limit always to the signal's engine limit.
export type CardChange = { type: "reduce_limit"; limit_cents: number } | { type: "pause_card" };

// Card's answer to an approved action (services/card, POST /customers/{id}/actions).
export interface CardReceipt {
  approval_id: string;
  case_id: string;
  customer_id: string;
  type: CardChange["type"];
  // "already_applied": Card had this approval on file and changed nothing again.
  status: "applied" | "already_applied";
  approved_by: string;
  approver_role: string;
  applied_at: string;
  details: {
    previous_card_status?: string;
    card_status?: string;
    previous_limit_cents?: number;
    limit_cents?: number;
    // The cut as basis points of the previous limit, worked out by Card.
    cut_bp?: number;
  };
}

// What code sent to Card for the case, with Card's receipt. null when nothing was sent.
export type CaseAction = CardChange & {
  case_id: string;
  customer_id: string;
  approved_by: string;
  approval_id: string;
  result?: CardReceipt;
};

// What approving one choice would do, decided by the backend: who comes next and what Card would receive.
export interface ApprovalOutcome {
  next: "manager" | "card" | "done" | "awaiting_documents";
  action: CardChange | null;
  // Why the choice needs a manager's authority: "pause", "cut_over_half"; [] when an analyst's is enough.
  manager_because?: string[];
}

// What the customer was asked for once request_documents was approved. The message is drafted by code from the case's
// signal, never by the model. Kept after the case closes.
export interface DocumentsRequest {
  asked_for: string[];
  message: string;
  // YYYY-MM-DD: the approval's date plus the policy's days to answer.
  due: string;
  requested_at: string;
  requested_by: string;
  // The requester's email when their token carried one.
  requested_by_name?: string | null;
}

export interface CaseView {
  case_id: string;
  customer_id: string;
  status: CaseStatus;
  waiting_for: WaitingFor;
  report: Report | null;
  decisions: Decision[];
  action: CaseAction | null;
  outage: Outage | null;
  // Card's words when it refused the approved action at the last attempt: sending it again will not help. null when
  // Card applied it or only failed to answer.
  card_refusal: string | null;
  // A review imported from earlier records, for a case this system never investigated. null for every case that ran.
  past_review?: PastReview | null;
  guidelines?: Guideline[];
  // Per choice the person can make at the step the case waits at. null when it waits for nobody or for a Card retry.
  approval_outcomes?: Record<string, ApprovalOutcome> | null;
  documents_request?: DocumentsRequest | null;
}

// One call to a specialist. Not the question's words: the backend keeps them out. A refused call was never sent, so
// it has no topic, separates or tests; its agent is null when it named a tool no specialist is behind.
export interface CasePathQuestion {
  tool?: string;
  agent: string | null;
  topic?: string;
  /** The causes whoever asked meant the answer to tell apart. */
  separates?: string[];
  /** The causes an answer on this topic can bear on, fixed by code. */
  tests?: string[];
  outcome: "answered" | "no_answer" | "refused";
  // What the question was about, when it named one record, one account or its own dates.
  record_id?: string;
  account_id?: string;
  start?: string;
  end?: string;
  /** The record ids that came back, in the order the backend wrote them. */
  returned?: string[];
  /** What the specialist answered, in its model's words: each finding with its confidence and the records behind it,
   * and what it could not determine. None on a refused call or one with no usable answer. */
  answer?: {
    findings: Array<{ claim: string; confidence: "high" | "medium" | "low"; evidence_ids: string[] }>;
    could_not_determine: string[];
  };
}

// One gap pass. Code looks first; the model looks only when code found nothing, and not while a plan runs ("none").
export interface CasePathGap {
  by: "code" | "model" | "none";
  gaps: string[];
  /** The follow-up each code gap names, in the order of gaps. Empty for the model's gaps. */
  asks?: Array<{ tool: string; topic: string; record_id?: string }>;
  /** The model's gaps code dropped because they stated a figure. */
  dropped?: number;
  /** How many calls (refused ones too) came before this pass. */
  at?: number;
  ended_by?: "done" | "prose" | "unreadable_call" | "round_limit" | "plan";
}

export interface CasePath {
  case_id: string;
  flow: "agent" | "fixed" | "fixed_rules" | null;
  policy_version: string | null;
  rounds: number;
  refusals: number;
  out_of_rounds: boolean;
  questions: CasePathQuestion[];
  // A search of past cases: what was searched and the case ids found, or refused. The ids can be other customers'.
  searches: Array<{ query?: string; returned?: string[]; refused?: boolean }>;
  gaps: CasePathGap[];
  // One entry per checked draft (draft_log): its labels and check codes, never its prose. The labels are null
  // when the draft did not parse as a report.
  drafts: Array<{
    draft: number;
    codes: string[];
    result: "passed" | "passed_low_confidence" | "refused" | "gave_up";
    recommendation: string | null;
    likely_cause: string | null;
    confidence: string | null;
    guideline_ids: string[];
    cited: string[];
  }>;
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

// A test customer an admin can send a signal for (GET /signals/examples).
export interface SignalExample {
  customer_id: string;
  name: string;
}

// A signal as the Orchestrator builds it for a test customer. Shown and sent back as it came.
export interface Signal {
  signal_id: string;
  customer_id: string;
  [field: string]: unknown;
}

// opened: a new case. joined: the customer already had an open case. duplicate: this signal_id was received before.
export interface SignalAccepted {
  case_id: string;
  status: CaseStatus;
  result: "opened" | "joined" | "duplicate";
}
