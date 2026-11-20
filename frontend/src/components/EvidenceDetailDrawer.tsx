import { useEffect } from "react";
import type { CasePath } from "../types";
import { dollarsFromCents, percentFromBp } from "../lib/format";

type PathRecord = CasePath["records"][number];

interface Props {
  /** The id of the record the reviewer clicked. null when nothing is open. */
  evidenceId: string | null;
  /** The resolved record, or null if the id has no matching record in /path. */
  record: PathRecord | null;
  onClose: () => void;
}

const KIND_LABELS: Record<string, string> = {
  cash_change: "Cash change",
  limit_change: "Limit change",
  card_position: "Card position",
  repayment_statuses: "Repayments",
  account_sync: "Account sync",
  review_history: "Review history",
  daily_balance: "Daily balance",
  payment_summary: "Payment summary",
  bank_transaction: "Bank transaction",
  weekly_inflow_series: "Weekly inflow series",
  balance_reconciliation: "Balance reconciliation",
  linked_account: "Linked account",
};

// A field whose name ends in _cents is money. _bp is basis points.
function renderValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (key.endsWith("_cents")) return dollarsFromCents(value, { precision: 2 });
    if (key.endsWith("_bp")) return percentFromBp(value);
    return String(value);
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value) || typeof value === "object") {
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

export function EvidenceDetailDrawer({ evidenceId, record, onClose }: Props) {
  // Close on Escape so the keyboard reviewer doesn't need the mouse.
  useEffect(() => {
    if (!evidenceId) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [evidenceId, onClose]);

  if (!evidenceId) return null;

  const data = record?.data ?? {};
  const entries = Object.entries(data);

  return (
    <div
      className="fixed inset-0 z-50 flex justify-end"
      onClick={onClose}
      role="dialog"
      aria-label={`Record ${evidenceId}`}
    >
      <div className="absolute inset-0 bg-slate-900/40" />
      <aside
        className="relative w-full max-w-md bg-white shadow-xl h-full overflow-y-auto"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="sticky top-0 bg-white border-b border-slate-200 px-5 py-4 flex items-center justify-between">
          <div>
            <div className="text-xs text-slate-500">
              {record ? (KIND_LABELS[record.kind] ?? record.kind) : "Record"}
            </div>
            <div className="font-mono text-sm text-slate-900">{evidenceId}</div>
          </div>
          <button
            onClick={onClose}
            className="text-slate-500 hover:text-slate-900 text-xl leading-none"
            aria-label="Close"
          >
            ×
          </button>
        </div>
        <div className="px-5 py-4">
          {!record ? (
            // Reports can cite records that /path does not return (e.g. `hist_`
            // or `xs_` prefixes from past-review search). The chip still opens
            // the drawer so the reviewer sees the id, with a clear note.
            <div className="text-sm text-slate-600">
              <p className="mb-2">
                No matching record in this case's investigation path.
              </p>
              <p className="text-slate-500">
                This evidence id was cited by the report but is not in the records the
                agent asked for on this run. It may be from an earlier review the agent
                looked up (<span className="font-mono">hist_*</span>) or from the case
                search (<span className="font-mono">xs_*</span>).
              </p>
            </div>
          ) : entries.length === 0 ? (
            <div className="text-sm text-slate-500 italic">No fields.</div>
          ) : (
            <dl className="divide-y divide-slate-100">
              {entries.map(([key, value]) => (
                <div key={key} className="py-2.5">
                  <dt className="text-[11px] uppercase tracking-wide text-slate-500">
                    {key}
                  </dt>
                  <dd className="text-sm text-slate-900 mt-0.5 break-words whitespace-pre-wrap">
                    {renderValue(key, value)}
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      </aside>
    </div>
  );
}
