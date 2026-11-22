import type { CaseStatus, WaitingFor } from "../types";

// No status uses violet (a hand-over's action tone in lib/format.ts ACTION_TONE), fuchsia or orange.
const STYLES: Record<CaseStatus, string> = {
  queued: "bg-slate-100 text-slate-700 ring-slate-200",
  investigating: "bg-sky-50 text-sky-700 ring-sky-200",
  waiting_for_data: "bg-amber-50 text-amber-700 ring-amber-200",
  needs_approval: "bg-slate-100 text-slate-700 ring-slate-300",
  needs_manager: "bg-slate-900 text-white ring-slate-900",
  awaiting_documents: "bg-sky-50 text-sky-700 ring-sky-200",
  done: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  rejected: "bg-slate-50 text-slate-600 ring-slate-200",
  failed: "bg-rose-50 text-rose-700 ring-rose-200",
};

const LABELS: Record<CaseStatus, string> = {
  queued: "Queued",
  investigating: "Investigating",
  waiting_for_data: "Waiting for data",
  needs_approval: "Analyst review",
  needs_manager: "Manager sign-off",
  awaiting_documents: "Waiting for documents",
  done: "Done",
  rejected: "Rejected",
  failed: "Failed",
};

// When the card system refuses or does not confirm an approved action, the case keeps the status of the step that
// approved it (cases.py decide), so the chip names the card system instead of a step that is already over. One label
// for a refusal and a missing answer alike: a queue row cannot tell them apart, and a refusal is not sent again.
const RETRY_LABEL = "Card system problem";
const RETRY_STYLE = "bg-amber-50 text-amber-700 ring-amber-200";

export function StatusChip({ status, waitingFor }: { status: CaseStatus; waitingFor?: WaitingFor }) {
  const retry = waitingFor === "retry";
  const style = retry ? RETRY_STYLE : (STYLES[status] ?? "bg-slate-50 text-slate-700 ring-slate-200");
  return (
    <span
      className={
        "inline-flex shrink-0 items-center whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset " +
        style
      }
    >
      {retry ? RETRY_LABEL : (LABELS[status] ?? status)}
    </span>
  );
}
