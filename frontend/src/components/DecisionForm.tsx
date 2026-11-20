import { useState, type FormEvent } from "react";
import type { CaseView, Report } from "../types";
import type { DecisionInput } from "../api/cases";

const DECIDED_OPTIONS = [
  { value: "keep_limit", label: "Keep limit" },
  { value: "reduce_limit", label: "Reduce limit" },
  { value: "pause_card", label: "Pause card" },
  { value: "request_documents", label: "Request documents" },
] as const;

interface Props {
  caseView: CaseView;
  disabled?: boolean;
  onSubmit: (input: DecisionInput) => void;
  submitting?: boolean;
}

function isHandOver(report: Report | null): boolean {
  if (!report) return false;
  return report.recommendation === "needs_human";
}

export function DecisionForm({ caseView, disabled, onSubmit, submitting }: Props) {
  const handOver = isHandOver(caseView.report);
  const [decision, setDecision] = useState<"approve" | "reject">("approve");
  const [note, setNote] = useState("");
  const [decided, setDecided] = useState<DecisionInput["decided"]>(undefined);

  const needsDecided = handOver && decision === "approve";

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const body: DecisionInput = { decision, note: note.trim() || undefined };
    if (needsDecided && decided) body.decided = decided;
    onSubmit(body);
  }

  const isDisabled = disabled ||
    (needsDecided && !decided) ||
    submitting;

  return (
    <form onSubmit={handleSubmit} className="bg-white rounded-xl border border-slate-200 p-6 space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-slate-900">Your decision</h2>
        <p className="text-sm text-slate-500 mt-1">
          {caseView.status === "needs_manager"
            ? "A manager needs to confirm a pause or a large cut."
            : "Approve to carry out the recommended action, or reject to close the case."}
        </p>
      </div>

      <div className="grid grid-cols-2 gap-3">
        {(["approve", "reject"] as const).map((choice) => (
          <button
            key={choice}
            type="button"
            onClick={() => setDecision(choice)}
            className={
              "border rounded-md px-3 py-2 text-sm font-medium transition " +
              (decision === choice
                ? choice === "approve"
                  ? "border-emerald-600 bg-emerald-50 text-emerald-700"
                  : "border-rose-600 bg-rose-50 text-rose-700"
                : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50")
            }
          >
            {choice === "approve" ? "Approve" : "Reject"}
          </button>
        ))}
      </div>

      {needsDecided && (
        <label className="block">
          <span className="text-sm font-medium text-slate-700">
            The agent handed this to a person. Pick the action you decide on:
          </span>
          <select
            value={decided ?? ""}
            onChange={(event) => setDecided((event.target.value || undefined) as DecisionInput["decided"])}
            className="mt-1 w-full border border-slate-300 rounded-md px-3 py-2"
            required
          >
            <option value="">Choose one…</option>
            {DECIDED_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      )}

      <label className="block">
        <span className="text-sm font-medium text-slate-700">Note (optional)</span>
        <textarea
          value={note}
          onChange={(event) => setNote(event.target.value)}
          className="mt-1 w-full border border-slate-300 rounded-md px-3 py-2"
          placeholder="Why you decided this. Kept on the record, at most 500 characters."
          rows={3}
          maxLength={500}
        />
      </label>

      <button
        type="submit"
        disabled={isDisabled}
        className={
          "w-full font-medium rounded-md px-4 py-2 text-white transition " +
          (decision === "approve"
            ? "bg-emerald-600 hover:bg-emerald-700"
            : "bg-rose-600 hover:bg-rose-700") +
          " disabled:opacity-50"
        }
      >
        {submitting ? "Sending…" : decision === "approve" ? "Approve" : "Reject"}
      </button>
    </form>
  );
}
