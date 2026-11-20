import type { CaseStatus } from "../types";

const STYLES: Record<CaseStatus, string> = {
  queued: "bg-slate-100 text-slate-700 ring-slate-200",
  investigating: "bg-sky-50 text-sky-700 ring-sky-200",
  waiting_for_data: "bg-amber-50 text-amber-700 ring-amber-200",
  needs_approval: "bg-violet-50 text-violet-700 ring-violet-200",
  needs_manager: "bg-fuchsia-50 text-fuchsia-700 ring-fuchsia-200",
  done: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  rejected: "bg-slate-50 text-slate-600 ring-slate-200",
  failed: "bg-rose-50 text-rose-700 ring-rose-200",
};

const LABELS: Record<CaseStatus, string> = {
  queued: "Queued",
  investigating: "Investigating",
  waiting_for_data: "Waiting for data",
  needs_approval: "Needs approval",
  needs_manager: "Needs manager",
  done: "Done",
  rejected: "Rejected",
  failed: "Failed",
};

export function StatusChip({ status }: { status: CaseStatus }) {
  const style = STYLES[status] ?? "bg-slate-50 text-slate-700 ring-slate-200";
  return (
    <span
      className={
        "inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset " +
        style
      }
    >
      {LABELS[status] ?? status}
    </span>
  );
}
