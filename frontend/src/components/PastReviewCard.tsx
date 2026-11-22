import type { PastReview } from "../types";
import { useAuth } from "../auth/AuthContext";
import {
  ACTION_TONE,
  CAUSE_LABELS,
  CONFIDENCE_STYLES,
  RECOMMENDATION_LABELS,
  STEP_LABELS,
  dollarsFromCents,
  formatDate,
  personLabel,
} from "../lib/format";
import { ReviewReasonChip } from "./ReviewReasonChip";

// What a closed review did, in the past tense. Only for a case that closed as done: a rejected one did not do it.
const DONE_LABELS: Record<string, string> = {
  keep_limit: "Kept limit",
  reduce_limit: "Reduced limit",
  pause_card: "Paused card",
  request_documents: "Requested documents",
};

/**
 * A review imported from earlier records (a seed), in place of a report: what it recorded, and plainly that this
 * system never investigated it. It is kept so later cases of the customer, and the people reviewing them, see the
 * history (a review that repeats is itself a signal).
 */
export function PastReviewCard({ past }: { past: PastReview }) {
  const { user } = useAuth();
  const decided = past.human_decision ?? past.recommendation;
  const label = decided ? (RECOMMENDATION_LABELS[decided] ?? decided) : null;
  // The outcome leads: what was done on a case that closed as done, else the record's own words for it.
  const headline =
    past.outcome === "done" && decided
      ? (DONE_LABELS[decided] ?? label)
      : `${label ?? "No outcome recorded"}, case ${past.outcome}`;
  const tone = past.outcome === "done" && decided ? ACTION_TONE[decided] : undefined;
  return (
    <section className="bg-white rounded-xl border border-slate-200 p-6 space-y-4">
      <div>
        <div className="text-xs uppercase tracking-wide text-slate-500">Past review, imported from earlier records</div>
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1">
          <span
            className={`rounded-md px-3 py-1 text-base font-semibold ring-1 ring-inset ${tone ?? ACTION_TONE.keep_limit}`}
          >
            {headline}
          </span>
          <span className="text-sm text-slate-500">
            Reviewed {formatDate(past.as_of)}
            {past.guideline_ids.length > 0 && `, under ${past.guideline_ids.join(", ")}`}
          </span>
        </div>
        <p className="text-sm text-slate-600 mt-2">
          This review was made before this system. It was not investigated here, so there is no report or
          investigation of its own: this is the record as it was kept. It is here so later reviews of this customer can
          see what happened then.
        </p>
      </div>

      <div className="flex items-center gap-6 flex-wrap text-sm">
        <div>
          <div className="text-xs text-slate-500">Limit then</div>
          <div className="text-slate-900 font-medium">
            {dollarsFromCents(past.current_limit_cents)} (engine proposed {dollarsFromCents(past.engine_limit_cents)})
          </div>
        </div>
        <div>
          <div className="text-xs text-slate-500">Linked cash in that week</div>
          <div className="text-slate-900 font-medium">
            {dollarsFromCents(past.cash_7d_ago_cents)} to {dollarsFromCents(past.cash_now_cents)}
          </div>
        </div>
        {past.review_reasons.length > 0 && (
          <div className="flex items-center gap-1.5 flex-wrap">
            {past.review_reasons.map((reason) => (
              <ReviewReasonChip key={reason} reason={reason} />
            ))}
          </div>
        )}
      </div>

      <div className="text-sm text-slate-800">
        <span className="text-slate-500">Cause recorded: </span>
        {past.likely_cause ? CAUSE_LABELS[past.likely_cause] ?? past.likely_cause : "none"}
        {past.confidence && (
          <span
            className={
              "ml-2 inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset " +
              (CONFIDENCE_STYLES[past.confidence] ?? "")
            }
          >
            {past.confidence} confidence
          </span>
        )}
      </div>

      {past.claims.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">What was found</div>
          <ul className="list-disc pl-5 text-sm text-slate-800 space-y-0.5">
            {past.claims.map((claim) => (
              <li key={claim}>{claim}</li>
            ))}
          </ul>
        </div>
      )}

      {past.decisions.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Decisions recorded</div>
          <ul className="space-y-1 text-sm">
            {past.decisions.map((decision, index) => {
              const step = decision.step ? (STEP_LABELS[decision.step] ?? decision.step).toLowerCase() : null;
              return (
                <li key={index} className="text-slate-800">
                  <span className="font-medium">{personLabel(decision.by, decision.by_name, user?.sub)}</span>
                  {/* A seed's people are ids from the earlier records, often with no email: said where they come from. */}
                  <span className="text-slate-500"> ({step ? `${step}, ` : ""}imported record)</span>{" "}
                  <span className={decision.decision === "approve" ? "text-emerald-700" : "text-rose-700"}>
                    {decision.decision === "approve" ? "approved" : "rejected"}
                  </span>
                  {decision.note && <span className="text-slate-600">: “{decision.note}”</span>}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}
