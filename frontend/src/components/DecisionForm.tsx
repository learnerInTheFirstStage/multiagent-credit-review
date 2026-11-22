import { useState, type FormEvent } from "react";
import type { ApprovalOutcome, CaseView } from "../types";
import type { DecisionInput } from "../api/cases";
import { useAuth } from "../auth/AuthContext";
import { REASON_LABELS, RECOMMENDATION_LABELS, becauseText, dollarsFromCents, personLabel } from "../lib/format";

type Decided = NonNullable<DecisionInput["decided"]>;
type Reason = NonNullable<DecisionInput["reason"]>;

// What a person can pick when the backend sends no approval_outcomes. It then says nothing of what each choice does.
const DOCUMENTS_CHOICES: Decided[] = ["keep_limit", "reduce_limit", "pause_card"];
const HAND_OVER_CHOICES: Decided[] = [...DOCUMENTS_CHOICES, "request_documents"];
// The fewest words a decision's rationale may have.
const MIN_RATIONALE_WORDS = 3;

// What a rejection can say was wrong. An approval of the agent's recommendation is "agree".
const REJECT_REASONS: Array<[Reason, string]> = [
  ["wrong_cause", "The report names the wrong cause."],
  ["wrong_action", "The cause is right, but the action is not."],
  ["missing_evidence", "The report does not cite enough to back it."],
  ["data_problem", "The bank or card data is wrong or out of date."],
  ["other", "Something else. Say why in the note."],
];

interface Props {
  caseView: CaseView;
  disabled?: boolean;
  onSubmit: (input: DecisionInput) => void;
  submitting?: boolean;
}

// A choice by name, with the limit a cut sets when the backend says what it is.
function choiceLabel(choice: string, outcome: ApprovalOutcome | undefined): string {
  const action = outcome?.action;
  if (choice === "reduce_limit" && action?.type === "reduce_limit") {
    return `Reduce limit to ${dollarsFromCents(action.limit_cents)}`;
  }
  return RECOMMENDATION_LABELS[choice] ?? choice;
}

// What approving does, in words, from the outcome the backend sent. Nothing for a next this app does not know. At the
// manager step the approval is the sign-off; before it, a manager's own approval is final by their authority.
function approvalText({ next, action, manager_because }: ApprovalOutcome, signOff: boolean): string | null {
  const inCard =
    action?.type === "pause_card"
      ? "pauses the card"
      : action?.type === "reduce_limit"
        ? `sets the limit to ${dollarsFromCents(action.limit_cents)}`
        : null;
  switch (next) {
    case "manager": {
      // Only an analyst's approval goes to a manager, and next says so: manager_because is set for a manager too.
      const because = becauseText(manager_because);
      return (
        (because
          ? `${because[0].toUpperCase()}${because.slice(1)}, so a manager must also approve.`
          : "This needs a manager's approval too.") +
        " Approving sends it to a manager" +
        (inCard ? `; after their approval the card system ${inCard}.` : ".")
      );
    }
    case "card":
      // A choice that needs a manager's authority goes straight to the card system only for a manager, whose
      // approval is final.
      if (signOff) {
        return inCard
          ? `Yours is the manager sign-off: the card system ${inCard} as soon as you approve.`
          : "Yours is the manager sign-off: it goes to the card system at once.";
      }
      if (manager_because?.length) {
        return inCard
          ? `You have manager authority, so your approval is final: the card system ${inCard} as soon as you approve.`
          : "You have manager authority, so your approval is final and goes to the card system at once.";
      }
      return inCard ? `Approving ${inCard} in the card system.` : "Approving sends this to the card system.";
    case "awaiting_documents":
      return "Approving drafts a request for documents for you to send the customer. Nothing is sent from the app. The limit stays unchanged while the case waits.";
    case "done":
      return "Approving records the decision. Nothing is sent to the card system.";
  }
  return null;
}

// What the submit button does, from the outcome approving it confirms. A plain "Approve" when the backend did not say.
function approveLabel(choice: Decided | undefined, outcome: ApprovalOutcome | undefined): string {
  if (!outcome) return "Approve";
  const { next, action } = outcome;
  if (next === "manager") return "Approve: send to a manager";
  if (next === "awaiting_documents") return "Approve: draft the document request";
  if (next === "done") return choice === "keep_limit" ? "Record: keep the limit" : "Record the decision";
  if (action?.type === "pause_card") return "Approve: pause the card";
  if (action?.type === "reduce_limit") return `Approve: set the limit to ${dollarsFromCents(action.limit_cents)}`;
  return "Approve";
}

// One option a person can pick (an outcome, or why they reject), with what it means.
function Choice({ name, label, line, checked, onPick }: {
  name: string;
  label: string;
  line: string | null;
  checked: boolean;
  onPick: () => void;
}) {
  return (
    <label
      className={
        "flex items-start gap-3 rounded-lg border px-3 py-2 cursor-pointer transition " +
        (checked ? "border-brand-500 bg-brand-50" : "border-slate-200 hover:bg-slate-50")
      }
    >
      <input type="radio" name={name} checked={checked} onChange={onPick} className="mt-1" />
      <span>
        <span className="block text-sm font-medium text-slate-900">{label}</span>
        {line && <span className="block text-sm text-slate-600 mt-0.5">{line}</span>}
      </span>
    </label>
  );
}

export function DecisionForm({ caseView, disabled, onSubmit, submitting }: Props) {
  const { user } = useAuth();
  const [decision, setDecision] = useState<"approve" | "reject">("approve");
  const [note, setNote] = useState("");
  const [decided, setDecided] = useState<Decided | undefined>(undefined);
  const [reason, setReason] = useState<Reason | undefined>(undefined);

  const step = caseView.waiting_for;
  // Only the analyst step picks the outcome of a hand-over: later steps confirm what was picked.
  const handOver = step === "analyst" && caseView.report?.recommendation === "needs_human";
  // The documents step records the outcome the customer's documents support. Rejecting it closes without a change.
  const documents = step === "documents";
  const picks = handOver || documents;
  const needsDecided = picks && decision === "approve";
  const last = caseView.decisions[caseView.decisions.length - 1];
  // Approving a person's pick (at a hand-over or the documents step, or a manager confirming either) agrees with no
  // recommendation of the agent's, so it sends no reason.
  const confirmsPick = picks || (step === "manager" && !!last?.decided);
  // Every decision records its rationale: credit decisions are sampled later for whether the analysis behind them was
  // documented. A few words at least, so "ok" or a key-mash is not taken for one.
  const rationaleWords = note.trim().split(/\s+/).filter(Boolean).length;
  const rationaleShort = rationaleWords < MIN_RATIONALE_WORDS;

  // The manager step is a second pair of eyes, so the person who approved the step before cannot confirm it.
  if (step === "manager" && user && last?.by === user.sub) {
    return (
      <div className="bg-amber-50 border border-amber-200 rounded-xl px-6 py-4 text-sm text-amber-800">
        {last.step === "documents" ? "You recorded what the documents showed" : "You approved the analyst step"}, so
        someone else must confirm it at the manager step.
      </div>
    );
  }

  const outcomes = caseView.approval_outcomes;
  const choices = Object.keys(outcomes ?? {}) as Decided[];
  const options = !picks ? [] : outcomes ? choices : documents ? DOCUMENTS_CHOICES : HAND_OVER_CHOICES;
  // The choice approving confirms: the one picked, else the only one the step offers.
  const approving = picks ? decided : choices.length === 1 ? choices[0] : undefined;
  const outcome = approving ? outcomes?.[approving] : undefined;
  // Said only where the backend says what approving does; for a hand-over, once an outcome is picked. The documents
  // step says it on each choice's own row.
  let consequence: string | null = null;
  if (outcomes && (!picks || handOver)) {
    if (decision === "reject") consequence = "Rejecting closes the case. Nothing changes on the card.";
    else if (outcome) consequence = approvalText(outcome, step === "manager");
  }
  // What the manager confirms: the outcome the step before approved, and why.
  const before = last?.decided ?? approving;
  // A manager's own approval of a choice that needs a manager's authority stands for both sign-offs. Not at the manager
  // step, where the analyst already signed.
  const signsBoth =
    decision === "approve" &&
    (step === "analyst" || step === "documents") &&
    outcome?.next === "card" &&
    !!outcome.manager_because?.length;

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    // The form shows only while the case waits for a person; without a step there is nothing to answer.
    if (!step) return;
    const body: DecisionInput = { decision, note: note.trim() || undefined, step };
    if (needsDecided && decided) body.decided = decided;
    // From the decision sent, so a reason picked before switching to approve is not sent with it.
    body.reason = decision === "reject" ? reason : confirmsPick ? undefined : "agree";
    onSubmit(body);
  }

  // What is left before the decision can be sent, said under the button while it is disabled.
  const missing: string[] = [];
  if (needsDecided && !decided) missing.push("Pick an outcome");
  if (decision === "reject" && !reason) missing.push(documents ? "Pick why you close it" : "Pick why you reject");
  if (rationaleShort) missing.push(`Write at least ${MIN_RATIONALE_WORDS} words on why`);
  const showMissing = !submitting && missing.length > 0;
  const isDisabled = disabled || missing.length > 0 || submitting;

  return (
    <form onSubmit={handleSubmit} className="bg-white rounded-xl border border-slate-200 p-6 space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-slate-900">
          {documents ? "Record what the documents showed" : "Your decision"}
        </h2>
        <p className="text-sm text-slate-500 mt-1">
          {step === "manager"
            ? "A manager needs to confirm a pause or a large cut."
            : documents
              ? "When the customer's documents come in, pick the outcome they support and approve it. Or close the case without a change."
              : handOver
                ? "The agent handed the case over without a recommendation. Approve the outcome you choose for this account, or reject the report to close the case."
                : "Approve the recommendation, or reject to close the case."}
        </p>
      </div>

      {step === "manager" && last?.decision === "approve" && (
        <p className="rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-700">
          {personLabel(last.by, last.by_name, user?.sub)}
          {last.by_role && ` (${last.by_role})`}
          {last.step === "documents" ? " recorded from the documents " : " approved "}
          <span className="font-medium">
            {before ? choiceLabel(before, outcomes?.[before]) : "the step before"}
          </span>
          {last.note ? `: “${last.note}”` : "."}
        </p>
      )}

      {/* Approve or reject first, as on every card. On a hand-over, approving then asks which outcome. */}
      {(!picks || handOver) && (
        <fieldset>
          <legend className="sr-only">Approve or reject</legend>
          <div className="inline-flex flex-wrap rounded-lg bg-slate-100 p-1">
            {(["approve", "reject"] as const).map((choice) => (
              <label key={choice} className="cursor-pointer">
                <input
                  type="radio"
                  name="decision"
                  value={choice}
                  checked={decision === choice}
                  onChange={() => setDecision(choice)}
                  className="sr-only peer"
                />
                <span className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-slate-600 peer-checked:bg-white peer-checked:font-medium peer-checked:text-slate-900 peer-checked:shadow-sm peer-focus-visible:ring-2 peer-focus-visible:ring-brand-700">
                  {decision === choice && <span aria-hidden="true">✓</span>}
                  {choice === "reject"
                    ? "Reject and close"
                    : approving
                      ? `Approve: ${choiceLabel(approving, outcome)}`
                      : "Approve"}
                </span>
              </label>
            ))}
          </div>
        </fieldset>
      )}

      {/* Approving a hand-over: which outcome, in the same toggle. What it does shows in the box below, as on every card. */}
      {handOver && decision === "approve" && (
        <fieldset>
          <legend className="text-sm font-medium text-slate-700 mb-2">Outcome to approve</legend>
          <div className="inline-flex flex-wrap rounded-lg bg-slate-100 p-1">
            {options.map((choice) => (
              <label key={choice} className="cursor-pointer">
                <input
                  type="radio"
                  name="outcome"
                  value={choice}
                  checked={decided === choice}
                  onChange={() => setDecided(choice)}
                  className="sr-only peer"
                />
                <span className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-slate-600 peer-checked:bg-white peer-checked:font-medium peer-checked:text-slate-900 peer-checked:shadow-sm peer-focus-visible:ring-2 peer-focus-visible:ring-brand-700">
                  {decided === choice && <span aria-hidden="true">✓</span>}
                  {choiceLabel(choice, outcomes?.[choice])}
                </span>
              </label>
            ))}
          </div>
        </fieldset>
      )}
      {documents && (
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium text-slate-700 mb-2">What the documents showed:</legend>
          {options.map((choice) => {
            const choiceOutcome = outcomes?.[choice];
            return (
              <Choice
                key={choice}
                name="outcome"
                label={choiceLabel(choice, choiceOutcome)}
                line={choiceOutcome ? approvalText(choiceOutcome, false) : null}
                checked={decision === "approve" && decided === choice}
                onPick={() => {
                  setDecision("approve");
                  setDecided(choice);
                }}
              />
            );
          })}
          <Choice
            name="outcome"
            label="Close without a change"
            line="Closes the case as rejected. Nothing is sent to the card system: the limit and the card stay as they are."
            checked={decision === "reject"}
            onPick={() => setDecision("reject")}
          />
        </fieldset>
      )}

      {decision === "reject" && (
        <fieldset className="space-y-2">
          <legend className="text-sm font-medium text-slate-700 mb-2">
            {documents ? "Why close it without a change?" : "Why are you rejecting?"} It is kept on the record with your
            note.
          </legend>
          {/* A hand-over made no recommendation, so its action cannot be wrong. */}
          {REJECT_REASONS.filter(([choice]) => !(handOver && choice === "wrong_action")).map(([choice, line]) => (
            <Choice
              key={choice}
              name="reason"
              label={REASON_LABELS[choice]}
              line={
                documents && choice === "other"
                  ? "Something else, such as the documents never came. Say why in the note."
                  : line
              }
              checked={reason === choice}
              onPick={() => setReason(choice)}
            />
          ))}
        </fieldset>
      )}

      {consequence && (
        <div className="rounded-md border border-slate-200 bg-slate-50 px-3 py-2 text-sm font-medium text-slate-800">
          {consequence}
        </div>
      )}

      <div>
        <label className="block">
          <span className="text-sm font-medium text-slate-700">Why you decided this (required)</span>
          <textarea
            value={note}
            onChange={(event) => setNote(event.target.value)}
            className="mt-1 w-full border border-slate-300 rounded-md px-3 py-2"
            placeholder="What the decision rests on, such as the records you checked. Kept on the record, at most 500 characters."
            rows={3}
            maxLength={500}
            required
            aria-describedby="decision-words"
          />
        </label>
        <p id="decision-words" className="text-xs text-slate-500 mt-1">
          {rationaleShort
            ? `${rationaleWords} of ${MIN_RATIONALE_WORDS} words`
            : `${rationaleWords} words`}
        </p>
      </div>

      <div className="space-y-2">
        <button
          type="submit"
          disabled={isDisabled}
          aria-describedby={showMissing ? "decision-missing" : undefined}
          className={
            "w-full font-medium rounded-md px-4 py-2 text-white transition " +
            (decision === "approve"
              ? "bg-slate-900 enabled:hover:bg-slate-800"
              : "bg-rose-700 enabled:hover:bg-rose-800") +
            " disabled:bg-slate-200 disabled:text-slate-500 disabled:cursor-not-allowed"
          }
        >
          {submitting
            ? "Sending…"
            : decision === "approve"
              ? approveLabel(approving, outcome)
              : documents
                ? "Close without a change"
                : "Reject and close the case"}
        </button>
        {showMissing && (
          <p id="decision-missing" className="text-sm text-slate-600">
            To continue: {missing.join(" · ")}
          </p>
        )}
        {signsBoth && (
          <p className="text-xs text-slate-500">
            {documents
              ? "Counts as the documents step and the manager sign-off."
              : "Counts as the analyst and the manager sign-off."}
          </p>
        )}
      </div>
    </form>
  );
}
