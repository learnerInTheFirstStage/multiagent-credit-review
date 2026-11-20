import type { CasePathQuestion } from "../types";

const TOPIC_LABELS: Record<string, string> = {
  transactions: "Transactions in the window",
  income: "Income trend",
  prior_year: "Prior year, same week",
  reconciliation: "Reconciliation of balances with transactions",
  accounts: "Linked accounts",
  payee_history: "Payee history",
  repayments: "Card repayments",
  paying_account: "Paying account",
  card_use: "Card use",
};

const AGENT_LABELS: Record<string, string> = {
  cash_flow: "Cash Flow",
  credit_behavior: "Credit Behavior",
};

const OUTCOME_STYLES: Record<string, string> = {
  answered: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  refused: "bg-rose-50 text-rose-700 ring-rose-200",
  outage: "bg-amber-50 text-amber-700 ring-amber-200",
  unknown: "bg-slate-100 text-slate-600 ring-slate-200",
};

interface Props {
  questions: CasePathQuestion[];
  /** Click a record id to open the drawer. */
  onEvidenceClick?: (id: string) => void;
}

export function InvestigationTimeline({ questions, onEvidenceClick }: Props) {
  if (!questions || questions.length === 0) {
    return (
      <div className="text-sm text-slate-500 italic">
        The agent did not ask any questions on this case.
      </div>
    );
  }
  return (
    <ol className="relative border-l border-slate-200 ml-2 space-y-4">
      {questions.map((q, idx) => (
        <li key={idx} className="ml-4 relative">
          <span className="absolute -left-[22px] top-4 flex h-3 w-3 items-center justify-center rounded-full bg-brand-500 ring-4 ring-white" />
          <div className="bg-white rounded-lg border border-slate-200 p-4">
            <div className="flex items-center justify-between gap-2 mb-2">
              <div className="text-xs text-slate-500">
                Round {idx + 1}
              </div>
              {(() => {
                const outcome = q.outcome && String(q.outcome).length > 0 ? q.outcome : "unknown";
                return (
                  <span
                    className={
                      "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset " +
                      (OUTCOME_STYLES[outcome] ?? OUTCOME_STYLES.unknown)
                    }
                  >
                    {outcome}
                  </span>
                );
              })()}
            </div>
            <div className="text-sm text-slate-900">
              <span className="font-medium">
                {AGENT_LABELS[q.agent ?? ""] ?? q.agent ?? "agent"}
              </span>{" "}
              was asked about{" "}
              <span className="font-medium">
                {TOPIC_LABELS[q.topic ?? ""] ?? q.topic ?? "a topic"}
              </span>
            </div>
            {q.returned && q.returned.length > 0 && (
              <div className="mt-2">
                <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1">
                  Records returned
                </div>
                <div className="flex flex-wrap gap-1.5">
                  {q.returned.map((id) => (
                    <button
                      key={id}
                      type="button"
                      onClick={() => onEvidenceClick?.(id)}
                      className="font-mono text-[11px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 hover:bg-brand-50 hover:text-brand-700 transition"
                    >
                      {id}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        </li>
      ))}
    </ol>
  );
}
