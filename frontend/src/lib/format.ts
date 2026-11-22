/**
 * Formatting helpers used across the review page.
 */

export const CAUSE_LABELS: Record<string, string> = {
  payroll_or_tax: "Payroll or tax",
  planned_vendor_payment: "Planned vendor payment",
  seasonal_dip: "Seasonal dip",
  funds_moved_out_of_view: "Funds moved out of view",
  revenue_loss: "Revenue loss",
  unexplained_outflow: "Unexplained outflow",
  no_clear_cause: "No clear cause",
  insufficient_data: "Insufficient data",
};

export const RECOMMENDATION_LABELS: Record<string, string> = {
  keep_limit: "Keep limit",
  reduce_limit: "Reduce limit",
  pause_card: "Pause card",
  request_documents: "Request documents",
  needs_human: "Needs human review",
};

// One tone per action, so a pause and a keep never look alike. Emerald stays for what Card applied.
export const ACTION_TONE: Record<string, string> = {
  keep_limit: "bg-slate-100 text-slate-800 ring-slate-300",
  request_documents: "bg-sky-50 text-sky-800 ring-sky-200",
  reduce_limit: "bg-amber-50 text-amber-800 ring-amber-200",
  pause_card: "bg-rose-50 text-rose-800 ring-rose-200",
  needs_human: "bg-violet-50 text-violet-800 ring-violet-200",
};

// Why a choice needs a manager's authority, in words, from the backend's codes (approval_outcomes' manager_because).
const MANAGER_BECAUSE: Record<string, string> = {
  pause: "pausing a card stops all spending",
  cut_over_half: "the cut is more than half the limit",
};

/** The reasons a manager's authority is needed, joined in words; "" when the backend gave none this app knows. */
export function becauseText(codes: string[] | undefined): string {
  return (codes ?? [])
    .map((code) => MANAGER_BECAUSE[code])
    .filter(Boolean)
    .join(", and ");
}

// The step a case waits at, or a decision was made at. The queue and the case page name them alike. The step after a
// Card failure is not called a retry: a refusal is never sent again.
export const STEP_LABELS: Record<string, string> = {
  analyst: "Analyst step",
  manager: "Manager step",
  documents: "Documents step",
  retry: "Card system step",
};

// Why a person approved or rejected. The form asks for one on a rejection; an approval is "agree".
export const REASON_LABELS: Record<string, string> = {
  agree: "Agree",
  wrong_cause: "Wrong cause",
  wrong_action: "Wrong action",
  missing_evidence: "Missing evidence",
  data_problem: "Data problem",
  other: "Other",
};

// Neutral for every level: a green "high confidence" next to a deep cut would read as good news.
export const CONFIDENCE_STYLES: Record<string, string> = {
  high: "bg-white text-slate-700 ring-slate-300",
  medium: "bg-white text-slate-700 ring-slate-300",
  low: "bg-white text-slate-700 ring-slate-300",
};

export const CONFIDENCE_LABELS: Record<string, string> = {
  high: "High confidence",
  medium: "Medium confidence",
  low: "Low confidence",
};

/**
 * Who wrote a report, for the case page and the report to say alike: code when it gave up on the drafts, the agent
 * when the report acts or the path says the hand-over was the agent's or low confidence, else null (the path, which
 * says who handed it over, did not load).
 */
export function reportAuthor(
  recommendation: string | undefined,
  handedOverBy: string | null | undefined,
): "agent" | "code" | null {
  if (handedOverBy === "gave_up") return "code";
  if (recommendation !== "needs_human" || handedOverBy) return "agent";
  return null;
}

// Causes that say the agent could not settle one.
const UNDETERMINED_CAUSES = new Set(["no_clear_cause", "insufficient_data"]);

/**
 * A hand-over because the cause could not be settled. KAN-50: the report then says so and shows no recommendation.
 * A specific action such as request_documents on such a cause is still a decision the agent made, so it is not one.
 */
export function undeterminedHandOver(likelyCause: string | undefined, recommendation: string | undefined): boolean {
  return recommendation === "needs_human" && !!likelyCause && UNDETERMINED_CAUSES.has(likelyCause);
}

/**
 * Whether the case page and the report show the agent's confidence: only on the agent's own report, and not on a
 * hand-over for a cause it could not settle, where it is confidence in nothing. A low-confidence hand-over keeps it,
 * because the low confidence is why it was handed over.
 */
export function showsConfidence(
  recommendation: string | undefined,
  likelyCause: string | undefined,
  handedOverBy: string | null | undefined,
): boolean {
  if (reportAuthor(recommendation, handedOverBy) !== "agent") return false;
  return handedOverBy === "low_confidence" || !undeterminedHandOver(likelyCause, recommendation);
}

// A record's kind in words, for the chips that cite it and the drawer's title. The bank's and Card's kinds
// (services/*/app/api/evidence.py) and the Orchestrator's own (facts.py).
const KIND_LABELS: Record<string, string> = {
  cash_change: "Cash change",
  limit_change: "Limit change",
  card_position: "Card position",
  repayment_statuses: "Repayments",
  account_sync: "Account sync",
  review_history: "Review history",
  earlier_review: "Earlier review",
  cash_recovery: "Cash recovery",
  account_match: "Account match",
  daily_balance: "Daily balance",
  payment_summary: "Payment summary",
  bank_transaction: "Bank transaction",
  weekly_inflow_series: "Weekly inflow series",
  balance_reconciliation: "Balance reconciliation",
  linked_account: "Linked account",
  payee_history: "Payee history",
  prior_year_comparison: "Prior year comparison",
  card_repayment: "Card repayment",
  funding_account_change: "Paying account change",
  card_balance: "Card balance",
  card_spend_series: "Card spend series",
  card_transaction: "Card transaction",
  repayment_summary: "Repayment summary",
  card_transaction_search: "Card transaction search",
  card_spend_breakdown: "Card spend breakdown",
};

/**
 * A linked account as a person knows it, "Chase Business Checking ••4821": its name at the bank and the last digits
 * of its number, from a linked_account record's data or the signal's account. null when it has no name, and the
 * caller shows the id.
 */
export function accountLabel(account: { name?: unknown; mask?: unknown }): string | null {
  if (typeof account.name !== "string" || !account.name) return null;
  return typeof account.mask === "string" && account.mask ? `${account.name} ••${account.mask}` : account.name;
}

/**
 * The name of each of the path's records, by id: a linked account as its bank names it (accountLabel), and any other
 * record its kind in words, numbered in path order where the path holds more than one of a kind. One map for the whole
 * page, so a record reads the same in every place that cites it and two records never read alike. A record of a kind
 * this app does not know, or one the path did not return, has no name here and shows as its id.
 */
export function recordLabels(
  records: Array<{ id: string; kind: string; data?: Record<string, unknown> }>,
): Record<string, string> {
  const named = ({ kind, data }: { kind: string; data?: Record<string, unknown> }) =>
    kind === "linked_account" && data ? accountLabel(data) : null;
  const total: Record<string, number> = {};
  for (const record of records) if (!named(record)) total[record.kind] = (total[record.kind] ?? 0) + 1;
  const seen: Record<string, number> = {};
  const labels: Record<string, string> = {};
  for (const record of records) {
    const { id, kind } = record;
    const name = named(record);
    if (name) {
      labels[id] = name;
      continue;
    }
    const label = KIND_LABELS[kind];
    if (!label) continue;
    seen[kind] = (seen[kind] ?? 0) + 1;
    labels[id] = total[kind] > 1 ? `${label} ${seen[kind]}` : label;
  }
  return labels;
}

const DAY = /^\d{4}-\d{2}-\d{2}$/;

/**
 * A day as the page shows it, "Nov 10, 2025". A bare YYYY-MM-DD reads as UTC midnight, so it is shown in UTC: in local
 * time it lands a day early west of Greenwich. A full timestamp shows the reader's own day. Text that is not a date
 * shows as it is.
 */
export function formatDate(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleDateString(
    undefined,
    DAY.test(value) ? { dateStyle: "medium", timeZone: "UTC" } : { dateStyle: "medium" },
  );
}

/** A moment as the page shows it, in the reader's own time zone: "Nov 21, 2026, 3:04 PM". */
export function formatDateTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function dollarsFromCents(
  cents: number | null | undefined,
  options: { precision?: number } = {},
): string {
  if (cents === null || cents === undefined) return "—";
  const dollars = cents / 100;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: options.precision ?? 0,
  }).format(dollars);
}

/** Basis points (10 000 = 100%) to a human percent. */
export function percentFromBp(bp: number | null | undefined, decimals = 2): string {
  if (bp === null || bp === undefined) return "—";
  return `${(bp / 100).toFixed(decimals)}%`;
}

export function percentDelta(from: number, to: number): string {
  if (from === 0) return "—";
  const raw = ((to - from) / from) * 100;
  const pct = Math.round(raw);
  // A tiny cut that rounds to zero should still read as negative, not +0%.
  if (pct === 0 && raw !== 0) return raw < 0 ? "<-1%" : "<+1%";
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct}%`;
}

/**
 * A person as the page names them: "you" for the reader, else their email when the record has one, else the sub
 * (an Auth0 sub reads "auth0|6aff…", so the email is far easier to recognise).
 */
export function personLabel(sub: string, name: string | null | undefined, readerSub: string | undefined): string {
  if (sub === readerSub) return "you";
  return name || sub;
}
