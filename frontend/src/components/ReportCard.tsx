import type { Report } from "../types";

const CAUSE_LABELS: Record<string, string> = {
  payroll_or_tax: "Payroll or tax",
  planned_vendor_payment: "Planned vendor payment",
  seasonal_dip: "Seasonal dip",
  funds_moved_out_of_view: "Funds moved out of view",
  revenue_loss: "Revenue loss",
  unexplained_outflow: "Unexplained outflow",
  no_clear_cause: "No clear cause",
  insufficient_data: "Insufficient data",
};

const RECOMMENDATION_LABELS: Record<string, string> = {
  keep_limit: "Keep limit",
  reduce_limit: "Reduce limit",
  pause_card: "Pause card",
  request_documents: "Request documents",
  needs_human: "Needs human review",
};

const CONFIDENCE_STYLES: Record<string, string> = {
  high: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  medium: "bg-amber-50 text-amber-700 ring-amber-200",
  low: "bg-rose-50 text-rose-700 ring-rose-200",
};

export function ReportCard({ report }: { report: Report }) {
  const likelyCause = report.likely_cause as string | undefined;
  const confidence = report.confidence as string | undefined;
  const recommendation = report.recommendation as string | undefined;

  return (
    <div className="bg-white rounded-xl border border-slate-200 p-6">
      <div className="flex items-start justify-between gap-6 mb-4">
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500">Likely cause</div>
          <div className="text-xl font-semibold text-slate-900 mt-1">
            {(likelyCause && CAUSE_LABELS[likelyCause]) ?? likelyCause ?? "—"}
          </div>
        </div>
        {confidence && (
          <span
            className={
              "inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset " +
              (CONFIDENCE_STYLES[confidence] ?? "bg-slate-50 text-slate-600 ring-slate-200")
            }
          >
            {confidence} confidence
          </span>
        )}
      </div>

      {report.summary && (
        <p className="text-slate-700 leading-relaxed mb-4">{report.summary}</p>
      )}

      {recommendation && (
        <div className="mb-4">
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Recommendation</div>
          <div className="inline-flex items-center rounded-md bg-brand-50 text-brand-700 px-3 py-1 text-sm font-semibold ring-1 ring-brand-200">
            {RECOMMENDATION_LABELS[recommendation] ?? recommendation}
          </div>
        </div>
      )}

      {Array.isArray(report.evidence) && report.evidence.length > 0 && (
        <div className="mb-4">
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">Evidence</div>
          <ul className="space-y-2">
            {report.evidence.map((item, idx) => (
              <li key={idx} className="border-l-2 border-brand-500 pl-3">
                <div className="text-slate-800">{item.claim}</div>
                <div className="text-xs text-slate-500 mt-0.5 font-mono">
                  cites: {item.evidence_ids.join(", ")}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {Array.isArray(report.ruled_out) && report.ruled_out.length > 0 && (
        <div className="mb-4">
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">Ruled out</div>
          <ul className="space-y-2">
            {report.ruled_out.map((item, idx) => (
              <li key={idx} className="text-sm">
                <span className="font-medium text-slate-900">
                  {(item.cause && CAUSE_LABELS[item.cause]) ?? item.cause}
                </span>
                <span className="text-slate-600"> — {item.reason}</span>
                <div className="text-xs text-slate-500 mt-0.5 font-mono">
                  cites: {item.evidence_ids.join(", ")}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.next_action && (
        <div className="mt-4 border-t pt-3">
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Next action</div>
          <div className="text-slate-700">{report.next_action}</div>
        </div>
      )}

      {Array.isArray(report.guideline_ids) && report.guideline_ids.length > 0 && (
        <div className="mt-3 text-xs text-slate-500">
          Guidelines: <span className="font-mono">{report.guideline_ids.join(", ")}</span>
        </div>
      )}
    </div>
  );
}
