import type { ReviewReason } from "../types";

const LABELS: Record<ReviewReason, string> = {
  large_cut: "Large cut",
  failed_repayment: "Failed repayment",
  incomplete_data: "Incomplete data",
  large_limit: "Large limit",
};

const STYLES: Record<ReviewReason, string> = {
  large_cut: "bg-amber-50 text-amber-700 ring-amber-200",
  failed_repayment: "bg-rose-50 text-rose-700 ring-rose-200",
  incomplete_data: "bg-slate-50 text-slate-700 ring-slate-200",
  large_limit: "bg-sky-50 text-sky-700 ring-sky-200",
};

export function ReviewReasonChip({ reason }: { reason: ReviewReason }) {
  const style = STYLES[reason] ?? "bg-slate-50 text-slate-700 ring-slate-200";
  const label = LABELS[reason] ?? reason;
  return (
    <span
      className={
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset " +
        style
      }
    >
      {label}
    </span>
  );
}
