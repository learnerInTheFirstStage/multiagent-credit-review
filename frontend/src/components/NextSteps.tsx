import { useState } from "react";
import { useAuth } from "../auth/AuthContext";
import { dollarsFromCents, formatDate } from "../lib/format";
import type { CaseView } from "../types";

// What happens after a decision, shown so the demo tells the whole business story. Simulated: nothing here is sent,
// and what a person ticks is kept in this browser only. The figures are code's (the signal's sig_ records and Card's
// receipt); the rules are cited where they come from.

interface Signal {
  cash?: { as_of?: string; cash_now_cents?: number; cash_7d_ago_cents?: number; fall_cents?: number };
  card?: { balance_cents?: number };
  repayments?: { repayments?: Array<{ due: string; amount_cents: number; status: string }> };
  reasons?: string[];
}

interface Props {
  caseView: CaseView;
  signal: Signal;
}

interface Ticked {
  noticeSent?: { at: string; by: string };
  repaymentReceived?: { at: string };
}

const NOTICE_DAYS = 30; // Regulation B, 12 CFR 1002.9: within 30 days of adverse action on a business of $1M or less

function readTicked(caseId: string): Ticked {
  try {
    return JSON.parse(localStorage.getItem(`next-steps:${caseId}`) ?? "{}") as Ticked;
  } catch {
    return {};
  }
}

function writeTicked(caseId: string, ticked: Ticked) {
  try {
    localStorage.setItem(`next-steps:${caseId}`, JSON.stringify(ticked));
  } catch {
    // A blocked store only loses the demo's ticks.
  }
}

// The reader's own calendar day, as the receipt's time above is shown, so the two never disagree across midnight UTC.
// Worked out as YYYY-MM-DD, which formatDate shows as that same day.
function localDay(iso: string, plusDays = 0): string {
  const day = new Date(iso);
  day.setDate(day.getDate() + plusDays);
  const pad = (n: number) => String(n).padStart(2, "0");
  return formatDate(`${day.getFullYear()}-${pad(day.getMonth() + 1)}-${pad(day.getDate())}`);
}

// The principal reasons, from the signal's own facts, at most four (Regulation B asks for specific reasons; an
// internal code such as "large_cut" is not one).
function noticeReasons(signal: Signal): string[] {
  const reasons: string[] = [];
  const { cash, repayments } = signal;
  // large_cut compares the limit with today's cash, so it can hold in a week when cash did not fall.
  if (
    signal.reasons?.includes("large_cut") &&
    cash?.cash_7d_ago_cents != null &&
    cash.cash_now_cents != null &&
    cash.cash_now_cents < cash.cash_7d_ago_cents
  ) {
    reasons.push(
      `The cash in your linked bank accounts fell from ${dollarsFromCents(cash.cash_7d_ago_cents)} to ` +
        `${dollarsFromCents(cash.cash_now_cents)} in the week to ${cash.as_of ? formatDate(cash.as_of) : "the signal's date"}.`,
    );
  }
  for (const repayment of repayments?.repayments ?? []) {
    if (repayment.status === "failed" || repayment.status === "late") {
      reasons.push(
        `Your card repayment of ${dollarsFromCents(repayment.amount_cents)} due ${formatDate(repayment.due)} ` +
          `${repayment.status === "failed" ? "failed" : "was late"}.`,
      );
    }
  }
  return reasons.slice(0, 4);
}

function Step({ n, title, children, done }: { n: number; title: string; children: React.ReactNode; done?: boolean }) {
  return (
    <li className="flex gap-3">
      <span
        className={
          "mt-0.5 h-6 w-6 shrink-0 rounded-full text-xs font-semibold flex items-center justify-center " +
          (done ? "bg-emerald-600 text-white" : "bg-slate-200 text-slate-700")
        }
      >
        {done ? "✓" : n}
      </span>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-slate-900">{title}</div>
        <div className="text-sm text-slate-700 mt-0.5 space-y-2">{children}</div>
      </div>
    </li>
  );
}

export function NextSteps({ caseView, signal }: Props) {
  const { user } = useAuth();
  const [ticked, setTicked] = useState<Ticked>(() => readTicked(caseView.case_id));
  const action = caseView.action;
  const receipt = action?.result;

  function tick(change: Ticked) {
    const next = { ...ticked, ...change };
    setTicked(next);
    writeTicked(caseView.case_id, next);
  }

  // Only once the case is over: a decision made and, for an action, Card's receipt in hand.
  if (!["done", "rejected"].includes(caseView.status)) return null;

  const monitoring = (
    <Step n={action && receipt ? 3 : 1} title="Monitoring goes on">
      The engine keeps watching this customer's linked cash every day. A new drop sends a new signal, which opens a
      new case.
    </Step>
  );

  if (!action || !receipt) {
    return (
      <section className="mt-6">
        <h2 className="text-lg font-semibold text-slate-900 mb-1">What happens next</h2>
        <p className="text-xs text-slate-500 mb-3">
          {action
            ? "The change was sent to the card system, but its receipt is not on the case. Check the card in the card " +
              "system before deciding whether the customer is owed a notice."
            : "The limit and the card did not change, so the customer is owed no notice of an adverse action."}
        </p>
        <ol className="space-y-4 bg-white rounded-xl border border-slate-200 p-5">{monitoring}</ol>
      </section>
    );
  }

  const pause = action.type === "pause_card";
  const appliedOn = localDay(receipt.applied_at);
  const due = localDay(receipt.applied_at, NOTICE_DAYS);
  const reasons = noticeReasons(signal);
  const failed = (signal.repayments?.repayments ?? []).filter((repayment) => repayment.status === "failed");
  // Regulation B, 12 CFR 1002.2(c)(2)(ii): action taken while the account is delinquent is not an adverse action,
  // whether it is a pause or a cut.
  const delinquencyQuestion = failed.length > 0;
  const balance = signal.card?.balance_cents;
  const overLimit = !pause && balance != null && balance > action.limit_cents ? balance - action.limit_cents : 0;
  const change = pause
    ? `We have paused spending on your card, effective ${appliedOn}.`
    : `We have lowered the credit limit on your card to ${dollarsFromCents(action.limit_cents)}, effective ${appliedOn}.`;
  const draft = [
    change,
    // No reason this code can state from the signal: the gap stays visible, as the agency's does below.
    reasons.length ? "The principal reasons:" : "The principal reasons: [add the reasons before sending]",
    ...reasons.map((reason) => `- ${reason}`),
    "You may ask us for a written statement of these reasons.",
    // Regulation B's ECOA notice, 12 CFR 1002.9(b)(1). The agency depends on the creditor, so the gap stays visible.
    "The Federal Equal Credit Opportunity Act prohibits creditors from discriminating against credit applicants on " +
      "the basis of race, color, religion, national origin, sex, marital status, age (provided the applicant has the " +
      "capacity to enter into a binding contract); because all or part of the applicant's income derives from any " +
      "public assistance program; or because the applicant has in good faith exercised any right under the Consumer " +
      "Credit Protection Act. The Federal agency that administers compliance with this law concerning this creditor " +
      "is [agency name and address].",
  ]
    .filter(Boolean)
    .join("\n");

  return (
    <section className="mt-6">
      <h2 className="flex items-center gap-2 text-lg font-semibold text-slate-900 mb-1">
        What happens next
        <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">Simulated</span>
      </h2>
      <p className="text-xs text-slate-500 mb-3">Nothing on this panel is sent. What you tick stays in this browser.</p>
      <ol className="space-y-4 bg-white rounded-xl border border-slate-200 p-5">
        <Step n={1} title={pause ? "Card paused" : "Limit lowered in the card system"} done>
          The card system applied it on {appliedOn} (receipt above).
          {overLimit > 0 && (
            <p>
              On {signal.cash?.as_of ? formatDate(signal.cash.as_of) : "the signal's date"} the customer owed{" "}
              {dollarsFromCents(balance)},{" "}
              {dollarsFromCents(overLimit)} above the new limit. A charge card's statement balance is due in full, so
              while they owe more than the limit they cannot spend on the card.
            </p>
          )}
          {pause && failed.length > 0 && (
            <p>
              The pause lasts until the {failed.map((repayment) => dollarsFromCents(repayment.amount_cents)).join(" and ")}{" "}
              repayment is made. Then a person reviews lifting it, and the card system resumes the card.
            </p>
          )}
        </Step>

        <Step n={2} title={`Tell the customer, by ${due}`} done={!!ticked.noticeSent}>
          <p>
            Adverse-action notice due by {due} (Reg B: {NOTICE_DAYS} days for a business with gross revenues of $1
            million or less last fiscal year)
          </p>
          {delinquencyQuestion && (
            <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-amber-900">
              This {pause ? "pause" : "limit cut"} follows a failed repayment. Regulation B does not count action taken
              because an account is delinquent as an adverse action, so compliance confirms whether this notice is owed.
            </p>
          )}
          <details>
            <summary className="cursor-pointer text-sm text-slate-600">When a notice is required</summary>
            <p className="mt-1">
              A lower limit or a paused card can be an adverse action on the account, but not when it is taken in
              connection with a current default or delinquency. For a business with gross revenues of $1 million or
              less in its last fiscal year, Regulation B asks for notice of the action within {NOTICE_DAYS} days, and
              the customer must get the specific reasons or be told they may ask for them. For a larger business it
              asks for notice of the action within a reasonable time, and written reasons only when the customer asks
              in writing within 60 days of the notice.
            </p>
          </details>
          <pre className="whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 p-3 text-xs text-slate-800 font-sans">
            {draft}
          </pre>
          <p className="text-xs text-slate-500">
            Drafted by code from the signal's figures, never by the model. A person checks it before it goes out.
          </p>
          {ticked.noticeSent ? (
            <p className="text-emerald-800">
              Marked sent (simulated) on {localDay(ticked.noticeSent.at)} by {ticked.noticeSent.by}.
            </p>
          ) : (
            <button
              type="button"
              onClick={() =>
                tick({ noticeSent: { at: new Date().toISOString(), by: user?.name ?? user?.sub ?? "you" } })
              }
              className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
            >
              Mark the notice sent
            </button>
          )}
        </Step>

        {pause && failed.length > 0 && (
          <Step n={3} title="When the repayment arrives" done={!!ticked.repaymentReceived}>
            {ticked.repaymentReceived ? (
              <p>
                Repayment marked received (simulated) on {localDay(ticked.repaymentReceived.at)}. Next, a person approves
                lifting the pause and the card system resumes the card. This demo does not send that to the card
                system.
              </p>
            ) : (
              <>
                <p>Nothing lifts the pause by itself: the repayment arriving puts the case back in front of a person.</p>
                <button
                  type="button"
                  onClick={() => tick({ repaymentReceived: { at: new Date().toISOString() } })}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50"
                >
                  The repayment arrived
                </button>
              </>
            )}
          </Step>
        )}

        {!(pause && failed.length > 0) && monitoring}
      </ol>
    </section>
  );
}
