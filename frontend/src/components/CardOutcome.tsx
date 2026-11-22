import { useState } from "react";
import type { CaseView } from "../types";
import type { DecisionInput } from "../api/cases";
import { useAuth } from "../auth/AuthContext";
import { RECOMMENDATION_LABELS, dollarsFromCents, formatDateTime, percentFromBp, personLabel } from "../lib/format";

// The outcome the approvals settled on: the one a person picked (a hand-over), else the report's recommendation.
function chosen({ decisions, report }: CaseView): string | undefined {
  return [...decisions].reverse().find((decision) => decision.decided)?.decided ?? report?.recommendation;
}

function label(choice: string | undefined): string | null {
  return choice ? (RECOMMENDATION_LABELS[choice] ?? choice) : null;
}

/** What happened at Card: its receipt for the action sent, or why nothing was sent. Nothing while the case is open. */
export function CardOutcome({ caseView }: { caseView: CaseView }) {
  const { user } = useAuth();
  const { action, status, decisions, outage, card_refusal: refusal } = caseView;
  // Card's receipt names the approver by sub; the decision that approved carries their email.
  const who = (sub: string) => personLabel(sub, decisions.find((decision) => decision.by === sub)?.by_name, user?.sub);

  if (action) {
    const receipt = action.result;
    if (!receipt) {
      return (
        <section className="mt-6">
          <h2 className="text-lg font-semibold text-slate-900 mb-2">Sent to the card system</h2>
          <div className="bg-white rounded-lg border border-slate-200 px-4 py-3 text-sm text-slate-700">
            {label(action.type)} was sent to the card system under approval{" "}
            <span className="font-mono">{action.approval_id}</span>. The card system's receipt is not on the case.
          </div>
        </section>
      );
    }
    const { details } = receipt;
    const pause = action.type === "pause_card";
    const before = pause ? details.previous_card_status : dollarsFromCents(details.previous_limit_cents);
    const after = pause ? details.card_status : dollarsFromCents(details.limit_cents);
    // Card's own report of the card now, against what was approved: the check an approver would otherwise make at Card.
    const matches = pause
      ? details.card_status === "paused"
      : action.type === "reduce_limit" && details.limit_cents === action.limit_cents;
    const approvedAs = pause ? "a paused card" : `a limit of ${dollarsFromCents(action.limit_cents)}`;
    return (
      <section className="mt-6">
        <h2 className="text-lg font-semibold text-slate-900 mb-2">Applied in the card system</h2>
        <div className="bg-white rounded-xl border border-slate-200 p-5">
          <div className="flex items-center gap-4 flex-wrap">
            <div>
              <div className="text-xs text-slate-500">{pause ? "Card status before" : "Limit before"}</div>
              <div className="text-xl font-semibold text-slate-900 capitalize">{before ?? "—"}</div>
            </div>
            <div className="text-slate-400 text-2xl font-light">→</div>
            <div>
              <div className="text-xs text-slate-500">{pause ? "Card status now" : "Limit now"}</div>
              <div className="text-xl font-semibold text-slate-900 capitalize">{after ?? "—"}</div>
            </div>
            <div className="ml-auto flex items-center gap-2">
              {!pause && details.cut_bp != null && (
                <span className="rounded-full bg-amber-50 px-2 py-0.5 text-xs text-amber-700 ring-1 ring-amber-200">
                  {percentFromBp(details.cut_bp, 0)} cut
                </span>
              )}
              <span
                className={
                  "rounded-full px-2 py-0.5 text-xs ring-1 " +
                  (receipt.status === "applied"
                    ? "bg-emerald-50 text-emerald-700 ring-emerald-200"
                    : "bg-sky-50 text-sky-700 ring-sky-200")
                }
              >
                {receipt.status === "applied" ? "Applied" : "Already applied"}
              </span>
            </div>
          </div>
          {receipt.status === "already_applied" && (
            <p className="text-sm text-slate-600 mt-3">
              The card system had already applied this approval when it was sent again, so nothing was changed twice.
            </p>
          )}
          <p className={"text-sm mt-3 " + (matches ? "text-slate-700" : "font-medium text-rose-700")}>
            {matches
              ? `The card system reports ${approvedAs}, as approved.`
              : `The card system reports ${after ?? "nothing"}, but ${approvedAs} was approved: check the card in the card system.`}
          </p>
          <dl className="mt-4 grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
            <div>
              <dt className="text-xs text-slate-500">Applied at</dt>
              <dd className="text-slate-800">{formatDateTime(receipt.applied_at)}</dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Approved by</dt>
              <dd className="text-slate-800">
                {who(receipt.approved_by)} ({receipt.approver_role})
              </dd>
            </div>
            <div>
              <dt className="text-xs text-slate-500">Approval id</dt>
              <dd className="text-slate-800 font-mono">{receipt.approval_id}</dd>
            </div>
          </dl>
        </div>
      </section>
    );
  }

  // A manager closed it after Card refused the action (nothing changed), or never answered (it may have applied it).
  if (status === "failed" && !outage && decisions[decisions.length - 1]?.step === "retry") {
    return (
      <section className="mt-6">
        <h2 className="text-lg font-semibold text-slate-900 mb-2">
          {refusal ? "Refused by the card system" : "Not confirmed by the card system"}
        </h2>
        <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 text-sm text-amber-800">
          {refusal ? (
            <>
              The card system refused the approved action, so nothing changed on the card, and a manager closed the
              case as failed. The card system said: {refusal}
            </>
          ) : (
            "The card system never confirmed the approved action, and a manager closed the case as failed. It may " +
            "have applied it anyway: check the customer's card in the card system before acting again."
          )}
        </div>
      </section>
    );
  }

  let why: string | null = null;
  if (status === "rejected") {
    // A reject at the documents step closes the case without a change, as the decision form and the list call it.
    why =
      decisions[decisions.length - 1]?.step === "documents"
        ? "The case was closed without a change after the documents, so nothing was sent to the card system. The " +
          "limit and the card stay as they were."
        : "The case was rejected, so nothing was sent to the card system. The limit and the card stay as they were.";
  } else if (status === "done") {
    const choice = chosen(caseView);
    if (choice === "keep_limit") why = "The decision was to keep the limit, so nothing was sent to the card system.";
    else if (choice === "request_documents")
      why =
        "The request for documents was recorded. Nothing was sent to the card system and the limit stays unchanged.";
    else if (choice === "reduce_limit" || choice === "pause_card")
      why = `${label(choice)} was recorded as the decision but not carried out: nothing was sent to the card system.`;
    else why = "The case was closed without an action, so nothing was sent to the card system.";
  }
  if (!why) return null;
  return (
    <section className="mt-6">
      <h2 className="text-lg font-semibold text-slate-900 mb-2">Nothing sent to the card system</h2>
      <div className="bg-white rounded-lg border border-slate-200 px-4 py-3 text-sm text-slate-700">{why}</div>
    </section>
  );
}

interface RetryProps {
  caseView: CaseView;
  onSubmit: (input: DecisionInput) => void;
  submitting?: boolean;
}

/**
 * In place of the decision form when an approval went to Card and Card did not confirm it (waiting_for "retry").
 * The backend's rule: only the last approver may send it again, only a manager may close the case as failed.
 */
export function CardRetryPanel({ caseView, onSubmit, submitting }: RetryProps) {
  const { user } = useAuth();
  const [note, setNote] = useState("");
  const last = caseView.decisions[caseView.decisions.length - 1];
  const approver = last ? personLabel(last.by, last.by_name, user?.sub) : "the approver";
  const refusal = caseView.card_refusal;
  // Card refused it: the same approval gets the same answer, so it is not offered again.
  const canResend = !refusal && !!user && !!last && last.by === user.sub;
  const canClose = user?.role === "manager";
  const approved = label(chosen(caseView));
  const what = approved ? <span className="font-medium text-slate-800">{approved}</span> : "The action";
  const by = (
    <>
      {approver}
      {last?.by_role && ` (${last.by_role})`}
    </>
  );

  return (
    <div className="bg-white rounded-xl border border-amber-300 p-6 space-y-5">
      {refusal ? (
        <div>
          <h2 className="text-lg font-semibold text-slate-900">The card system refused the action</h2>
          <p className="text-sm text-slate-600 mt-1">
            {what} was approved by {by}, but the card system refused it, so nothing changed on the card. Sending it
            again would get the same answer. A manager can close the case as failed.
          </p>
          <p className="mt-2 rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">{refusal}</p>
        </div>
      ) : (
        <div>
          <h2 className="text-lg font-semibold text-slate-900">The card system has not confirmed the action</h2>
          <p className="text-sm text-slate-600 mt-1">
            {what} was approved by {by}, but the card system did not confirm it carried it out. It may have applied it
            and lost its answer, so do not assume either way.
          </p>
        </div>
      )}

      {canResend && (
        <div>
          <button
            type="button"
            disabled={submitting}
            onClick={() => onSubmit({ decision: "approve", step: "retry" })}
            className="w-full font-medium rounded-md px-4 py-2 text-white bg-slate-900 enabled:hover:bg-slate-800 transition disabled:opacity-50"
          >
            {submitting ? "Sending…" : "Send it to the card system again"}
          </button>
          <p className="text-xs text-slate-500 mt-1">
            Sends the same approval. The card system applies each approval once: if it already did, nothing changes
            twice and the page shows its receipt.
          </p>
        </div>
      )}

      {canClose && (
        <div className="space-y-2">
          <label className="block">
            {/* Required: a failure at Card is not a judgment of the report, so none of the reject reasons fits it.
                The note is the record of why the case was closed and what is known about the card. */}
            <span className="text-sm font-medium text-slate-700">Why you are closing it (required)</span>
            <textarea
              value={note}
              onChange={(event) => setNote(event.target.value)}
              className="mt-1 w-full border border-slate-300 rounded-md px-3 py-2"
              placeholder="What you know about the card in the card system. Kept on the record, at most 500 characters."
              rows={2}
              maxLength={500}
            />
          </label>
          <button
            type="button"
            disabled={submitting || !note.trim()}
            onClick={() => onSubmit({ decision: "reject", note: note.trim(), step: "retry" })}
            className="w-full font-medium rounded-md px-4 py-2 text-white bg-rose-600 hover:bg-rose-700 transition disabled:opacity-50"
          >
            {submitting ? "Sending…" : "Close the case as failed"}
          </button>
          <p className="text-xs text-slate-500">
            {refusal
              ? "Nothing more is sent to the card system. It refused the action, so the card and its limit are as they were."
              : "Nothing more is sent to the card system. It may have applied the action, so someone must check the customer's card in the card system."}
          </p>
        </div>
      )}

      {((!canResend && !refusal) || !canClose) && (
        <p className="text-sm text-slate-500">
          {!canResend && !refusal && `Only ${approver} can send it again.`}{" "}
          {!canClose && "Only a manager can close it as failed."}
        </p>
      )}
    </div>
  );
}
