import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { postDecision, postRetry, readCase, readPath } from "../api/cases";
import type { DecisionInput } from "../api/cases";
import { StatusChip } from "../components/StatusChip";
import { ReportCard } from "../components/ReportCard";
import { DecisionForm } from "../components/DecisionForm";
import { CardOutcome, CardRetryPanel } from "../components/CardOutcome";
import { DocumentsRequestPanel } from "../components/DocumentsRequestPanel";
import { EngineLimitHeader } from "../components/EngineLimitHeader";
import { PastReviewCard } from "../components/PastReviewCard";
import { NextSteps } from "../components/NextSteps";
import { InvestigationTimeline } from "../components/InvestigationTimeline";
import { EvidenceDetailDrawer } from "../components/EvidenceDetailDrawer";
import { ErrorBanner } from "../components/ErrorBanner";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import {
  ACTION_TONE,
  CONFIDENCE_LABELS,
  CONFIDENCE_STYLES,
  REASON_LABELS,
  RECOMMENDATION_LABELS,
  STEP_LABELS,
  becauseText,
  dollarsFromCents,
  formatDateTime,
  percentFromBp,
  personLabel,
  recordLabels,
  showsConfidence,
} from "../lib/format";
import type { CasePath, CaseStatus, CaseView, WaitingFor } from "../types";

const ACTIVE_STATUSES = new Set(["queued", "investigating", "waiting_for_data"]);

// Who handed a case over to a person, from the path. Code's hand-overs are not the agent's recommendation.
const HAND_OVER_LINES: Record<string, string> = {
  report: "Handed over by the agent",
  low_confidence: "Handed over by the rule check: the agent's confidence was low",
  gave_up: "Handed over by the rule check: the drafts failed it",
};

// The orchestrator writes `case.status` in the DB and the LangGraph state's own
// `status` (which gates the report in `_view()`) in two separate steps. For a
// brief window a GET /cases/{id} can return a settled status with the report
// still null. Only `needs_approval` and `needs_manager` can hit this race: both
// transitions happen right after the agent commits the report. `done` and
// `rejected` only come from a decision after a report exists, so the race is
// already resolved by then. `failed` can legitimately have no report (the
// agent crashed, or a specialist failed with no fallback) and seeded reviews
// read as `done` with `report: null` by design, so excluding those statuses
// avoids infinite polling on legitimate-no-report cases. Self-heals in one or
// two ticks.
const RACE_STATUSES = new Set(["needs_approval", "needs_manager"]);

/** True while status + null-report hints at the DB/LangGraph commit race. */
function isFinalising(data: { status: string; report: unknown | null } | undefined) {
  return !!data && RACE_STATUSES.has(data.status) && !data.report;
}

/**
 * What the decision just made did, from the case the backend returned. "A manager" only where the backend moved the
 * case to the manager step, and a receipt only where Card's is on the case.
 */
function decidedLine(updated: CaseView, input: DecisionInput): string {
  if (updated.status === "needs_manager") return "Approved. Sent to a manager for sign-off.";
  if (updated.status === "awaiting_documents") return "Approved. The document request is drafted below.";
  if (updated.status === "rejected") {
    return `${input.step === "documents" ? "Closed without a change." : "Rejected."} Nothing was sent to the card system.`;
  }
  if (updated.status === "failed") return "Closed as failed. Nothing more is sent to the card system.";
  const approved = input.step === "retry" ? "Sent again." : "Approved.";
  if (updated.action?.result) return `${approved} The card system's receipt is below.`;
  if (updated.action) return `${approved} Sent to the card system.`;
  if (updated.status === "done") return `${approved} Nothing was sent to the card system.`;
  return "Decision recorded.";
}

type PathRecord = CasePath["records"][number];

export default function CaseDetailPage() {
  const { caseId = "" } = useParams<{ caseId: string }>();
  const { user } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const qc = useQueryClient();
  const [selectedEvidence, setSelectedEvidence] = useState<string | null>(null);
  const [showRawPath, setShowRawPath] = useState(false);
  // The raw path is for debugging only, behind ?debug: the scripted stack serves a production build, so not on DEV.
  const [searchParams] = useSearchParams();
  const debug = searchParams.has("debug");
  // Why the last decision did not go through, and the step the case was at once the page re-read it.
  const [decisionError, setDecisionError] = useState<{ message: string; at: WaitingFor | undefined } | null>(null);
  // What the reader's last decision did, and where it left the case.
  const [justDecided, setJustDecided] = useState<{ message: string; status: CaseStatus; at: WaitingFor } | null>(null);
  const outcomeRef = useRef<HTMLDivElement>(null);
  const documentsRef = useRef<HTMLDivElement>(null);
  const noticeRef = useRef<HTMLParagraphElement>(null);

  const caseQuery = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => readCase(caseId),
    enabled: !!caseId,
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 3_000;
      // Poll while the case is in motion, stop once a person needs to act or it's closed.
      if (ACTIVE_STATUSES.has(data.status)) return 3_000;
      // Keep polling across the finalise race (settled status but report still null).
      if (isFinalising(data)) return 3_000;
      return false;
    },
  });

  // Path is always fetched: it feeds the engine-limit header, the timeline,
  // and the lookup for clickable evidence ids. Polls on the same cadence as
  // the case while the agent is working, or while we're inside the finalise
  // race so the timeline keeps growing in step with the report.
  const caseData = caseQuery.data;
  const finalising = isFinalising(caseData);
  const caseStatus = caseData?.status;
  const pathQuery = useQuery({
    queryKey: ["case-path", caseId],
    queryFn: () => readPath(caseId),
    enabled: !!caseId,
    refetchInterval:
      caseData && (ACTIVE_STATUSES.has(caseData.status) || finalising) ? 3_000 : false,
  });

  // The hand-over and the last draft reach the path with the checked report, often after the path's last poll. Fetch
  // it once more when the case comes to rest, after the agent's run and after the finalise race (whose status does
  // not change): the event stream does this too, but not in polling mode.
  const inMotion = caseStatus ? ACTIVE_STATUSES.has(caseStatus) || finalising : undefined;
  const wasInMotion = useRef(inMotion);
  useEffect(() => {
    const previous = wasInMotion.current;
    wasInMotion.current = inMotion;
    if (previous && inMotion === false) qc.invalidateQueries({ queryKey: ["case-path", caseId] });
  }, [inMotion, caseId, qc]);

  const decisionMutation = useMutation({
    mutationFn: (input: DecisionInput) => postDecision(caseId, input),
    // Cleared here, not when the status changes: the re-read below can move the case on, and its message must stay.
    onMutate: () => {
      setDecisionError(null);
      setJustDecided(null);
    },
    onSuccess: (updated, input) => {
      qc.setQueryData(["case", caseId], updated);
      qc.invalidateQueries({ queryKey: ["cases"] });
      setJustDecided({ message: decidedLine(updated, input), status: updated.status, at: updated.waiting_for });
    },
    onError: async (err, input) => {
      // An approval Card did not carry out is still recorded: re-read the case so the Card retry panel shows.
      // Awaited, so the message below knows where the case is now. The form stays pending until then.
      await qc.invalidateQueries({ queryKey: ["case", caseId] });
      const view = qc.getQueryData<CaseView>(["case", caseId]);
      const at = view?.waiting_for;
      // Card did not carry out an approval made at this step: the panel that now shows says so, and what can be done,
      // in the page's words. The Orchestrator's message would say it again in the API's (reject, cents). A resend from
      // that panel that fails again still says why.
      if (at === "retry" && input.step !== "retry") return;
      // A 409 also answers a request the step itself refuses: only a case that left the step was decided first. If
      // the re-read failed, the step looks unchanged and the Orchestrator's own message shows.
      const movedOn = at !== input.step;
      const lost = !(err instanceof ApiError); // no answer reached the page, so the request may have gone through
      const conflict = err instanceof ApiError && err.status === 409;
      // The reader's own decision from another tab, or one whose answer was lost. Card's 502s already say it is kept.
      const mine = !!user && view?.decisions.findLast((decision) => decision.step === input.step)?.by === user.sub;
      let message: string;
      if (movedOn && mine && (lost || conflict)) {
        message = "Your decision at this step is recorded. The page now shows where the case is.";
      } else if (movedOn && lost) {
        message = "The answer did not reach this page. It now shows where the case is.";
      } else if (movedOn && conflict) {
        message = "Someone else decided this step first. The page now shows their decision.";
      } else {
        message = err instanceof ApiError ? err.message : String(err);
      }
      setDecisionError({ message, at });
    },
  });

  // Runs the case again from the question Bank or Card did not answer. The answer says "investigating", so polling
  // starts again.
  const retryMutation = useMutation({
    mutationFn: () => postRetry(caseId),
    onSuccess: (updated) => {
      qc.setQueryData(["case", caseId], updated);
      qc.invalidateQueries({ queryKey: ["cases"] });
      qc.invalidateQueries({ queryKey: ["case-path", caseId] });
    },
    // A refusal means the case moved on, perhaps by someone else's retry. A failed case is not polled: re-read it.
    onError: () => qc.invalidateQueries({ queryKey: ["case", caseId] }),
  });

  // A refusal is about the status it was clicked at: drop it once the case moves on.
  const resetRetry = retryMutation.reset;
  useEffect(() => {
    resetRetry();
  }, [caseStatus, resetRetry]);

  // Shown while the case is where the decision left it: a decision from another browser moves it on.
  const decided =
    justDecided && caseData?.status === justDecided.status && caseData.waiting_for === justDecided.at
      ? justDecided
      : null;
  // The form the person pressed goes away, so bring the confirmation and Card's receipt into view, or the drafted
  // request for documents, which shows above the documents step's form. After the commit, not in onSuccess: the new
  // view can reach the page a tick after the mutation settles.
  useEffect(() => {
    if (!decided) return;
    const target = (decided.status === "awaiting_documents" ? documentsRef.current : null) ?? outcomeRef.current;
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    target?.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
    // The pressed button is gone: focus the line, so a screen reader reads it (a live region added with its text
    // often is not announced) and focus does not fall to the page.
    noticeRef.current?.focus({ preventScroll: true });
  }, [decided]);

  const caseView = caseQuery.data;

  const recordById = useMemo(() => {
    const map: Record<string, PathRecord> = {};
    const records = pathQuery.data?.records ?? [];
    for (const record of records) map[record.id] = record;
    return map;
  }, [pathQuery.data]);
  // One name per record for the whole page: the report, the timeline and the drawer number a kind alike.
  const labelById = useMemo(() => recordLabels(pathQuery.data?.records ?? []), [pathQuery.data]);

  type HeaderProps = React.ComponentProps<typeof EngineLimitHeader>;
  const signalRecords = {
    limit: recordById["sig_limit"]?.data as HeaderProps["limit"],
    cash: recordById["sig_cash"]?.data as HeaderProps["cash"],
    card: recordById["sig_card"]?.data as HeaderProps["card"],
    repayments: recordById["sig_repayments"]?.data as HeaderProps["repayments"],
    accounts: recordById["sig_accounts"]?.data as HeaderProps["accounts"],
  };

  // Stable callback so the drawer's Esc listener is not re-registered on every
  // 3 s polling render.
  const closeDrawer = useCallback(() => setSelectedEvidence(null), []);

  const mayAct = user?.role === "analyst" || user?.role === "manager";
  // An approval Card did not confirm keeps its approval status: the retry panel shows instead of the form.
  const cardRetry = caseView?.waiting_for === "retry";
  const needsDecision =
    caseView &&
    !finalising &&
    !cardRetry &&
    ((caseView.status === "needs_approval" && mayAct) ||
      (caseView.status === "awaiting_documents" && mayAct) ||
      (caseView.status === "needs_manager" && user?.role === "manager"));
  // A failed case without an outage was closed by a person, not for want of Bank or Card: the backend refuses it.
  const mayRetry =
    mayAct && !!caseView?.outage && (caseView.status === "waiting_for_data" || caseView.status === "failed");

  if (caseQuery.isLoading) {
    return <div className="text-center text-slate-500 py-10">Loading case…</div>;
  }
  // When nothing was ever loaded, or the case is gone or no longer the reader's: no retry brings those back. Any other
  // failed poll of a loaded case keeps it on screen, with the banner below.
  const err = caseQuery.error;
  const final = err instanceof ApiError && (err.status === 403 || err.status === 404);
  if (!caseView || final) {
    let detail: string;
    if (err instanceof ApiError) {
      if (err.status === 404) detail = "This case does not exist or has been removed.";
      else if (err.status === 403) detail = "You don't have access to this customer.";
      else detail = err.message;
    } else {
      detail = err ? String(err) : "Unknown error.";
    }
    return (
      <div className="text-center py-10">
        <p className="text-red-700">Could not load the case.</p>
        <p className="text-slate-500 text-sm mt-1">{detail}</p>
        <Link to="/" className="text-brand-700 hover:underline text-sm mt-4 inline-block">
          Back to queue
        </Link>
      </div>
    );
  }

  const agentWorking = caseView.status === "queued" || caseView.status === "investigating";
  // In the fixed flows code asks every question and no agent investigates. Until the path is read, neither is said.
  const flowKnown = pathQuery.data !== undefined;
  const fixedPlan = pathQuery.data?.flow === "fixed" || pathQuery.data?.flow === "fixed_rules";
  // Who handed it over comes from the path, and on a live case it reaches the path after its last poll, so wait for a
  // path read no older than the case read: the effect above starts it when the case comes to rest. Decided here, not in
  // that effect, so the report does not paint first. Without it the hand-over banner pops in late, and on a case code
  // gave up on, the placeholder cause and confidence show first.
  const handOverPending =
    caseView.report?.recommendation === "needs_human" &&
    !pathQuery.data?.handed_over_by &&
    !pathQuery.isError &&
    (pathQuery.isPending || pathQuery.isFetching || pathQuery.dataUpdatedAt < caseQuery.dataUpdatedAt);

  // The verdict strip shows with the report, and says in one line what the step decides.
  const recommendation = caseView.report?.recommendation;
  const showVerdict = !!recommendation && !agentWorking && !finalising && !handOverPending;
  const handedOverBy = pathQuery.data?.handed_over_by;
  const lastDecision = caseView.decisions[caseView.decisions.length - 1];
  // Whether the reader decides this step: the person who approved the step before cannot confirm it, and the form
  // tells them so.
  const decidesHere = !!needsDecision && !(caseView.waiting_for === "manager" && !!user && lastDecision?.by === user.sub);
  // After a hand-over or the documents step, the manager step confirms what a person picked, not the recommendation; a
  // case done on such a pick carried out the pick, and one Card did not confirm was sent it. The backend's one outcome
  // at the manager step is that pick, as the form reads it.
  const pick = [...caseView.decisions].reverse().find((decision) => !!decision.decided);
  const showsPick =
    caseView.waiting_for === "manager"
      ? !!lastDecision?.decided
      : (caseView.status === "done" || caseView.status === "failed" || cardRetry) && !!pick;
  const shown = showsPick
    ? (Object.keys(caseView.approval_outcomes ?? {})[0] ?? pick?.decided ?? undefined)
    : recommendation;
  let label: string | null = "Agent recommends";
  let verdict = shown ? (RECOMMENDATION_LABELS[shown] ?? shown) : "";
  if (showsPick) {
    label = pick?.step === "documents" ? "What the documents showed" : "Picked at the analyst step";
  } else if (recommendation === "needs_human") {
    // Who handed it over, not a recommendation: none was made. Said plainly when the path did not load.
    label = null;
    verdict = HAND_OVER_LINES[handedOverBy ?? ""] ?? "Handed over to a person";
  }
  if (shown === "reduce_limit") {
    // The amount is what approving sends Card, from the backend. A closed case has no outcomes left to approve: there it
    // is the action that was sent. Card did not carry out the one a Card retry waits on, so neither is kept: a cut
    // always sets the engine's limit (build.py action_for), from the signal.
    const sent = caseView.approval_outcomes?.reduce_limit?.action ?? caseView.action;
    const engineLimit = signalRecords.limit?.engine_limit_cents;
    const cut =
      sent ?? (cardRetry && engineLimit != null ? { type: "reduce_limit" as const, limit_cents: engineLimit } : null);
    if (cut?.type === "reduce_limit") {
      verdict = `Reduce limit to ${dollarsFromCents(cut.limit_cents)}`;
      // The signal's own share of the current limit, said only when it is about this amount.
      const limit = signalRecords.limit;
      if (limit?.cut_share_bp != null && limit.engine_limit_cents === cut.limit_cents) {
        verdict += ` (${percentFromBp(limit.cut_share_bp, 0)} cut)`;
      }
    }
  }
  // Who signs, from what the backend says approving does for this reader. "Needs a manager" only when approving goes to
  // one: a manager's own approval sets manager_because too, and is final. Where the reader picks the outcome, as the
  // form has them do, that is said instead.
  const outcome = shown ? caseView.approval_outcomes?.[shown] : undefined;
  let stepLine: string | null = null;
  if (decidesHere && outcome?.next === "manager") {
    const because = becauseText(outcome.manager_because);
    stepLine = because ? `Needs a manager too: ${because}` : "Needs a manager too";
  } else if (decidesHere && outcome?.next === "card" && caseView.waiting_for === "manager") {
    stepLine = "Yours is the manager sign-off";
  } else if (decidesHere && outcome?.next === "card" && outcome.manager_because?.length) {
    stepLine = "Your approval is final (manager authority)";
  } else if (decidesHere && caseView.waiting_for === "documents") {
    stepLine = "Next: record what the documents showed";
  } else if (decidesHere && caseView.waiting_for === "analyst" && recommendation === "needs_human") {
    stepLine = "No recommendation";
  } else if (cardRetry) {
    stepLine = caseView.card_refusal
      ? "The card system refused the approved action"
      : "The card system has not confirmed the approved action";
  }
  // The step in the header. After a Card failure it is named by what Card answered, as the panel below names it: a
  // refusal is not sent again.
  const stepName =
    caseView.waiting_for === "retry"
      ? caseView.card_refusal
        ? "Card refused"
        : "Card not confirmed"
      : caseView.waiting_for && (STEP_LABELS[caseView.waiting_for] ?? caseView.waiting_for);
  // The agent's confidence is in its own recommendation, so it is not shown with a person's pick. Otherwise
  // showsConfidence decides, the rule the report card uses too.
  const confidence = caseView.report?.confidence;
  const showConfidence =
    !!confidence && !showsPick && showsConfidence(recommendation, caseView.report?.likely_cause, handedOverBy);
  // A cut is possible when the report recommends one, a person picks the outcome (a hand-over or the documents step),
  // or a decision already picked one.
  const cutOnTheTable =
    ["reduce_limit", "needs_human"].includes(caseView.report?.recommendation ?? "") ||
    caseView.waiting_for === "documents" ||
    caseView.decisions.some((decision) => decision.decided === "reduce_limit");
  // What the last decision did: over Card's receipt, or over the request for documents it drafted.
  const decidedAtDocuments = decided?.status === "awaiting_documents" && !!caseView.documents_request;
  const decidedNotice = decided && (
    <p
      ref={noticeRef}
      role="status"
      tabIndex={-1}
      className="rounded-lg bg-emerald-50 px-4 py-3 text-sm text-emerald-800 ring-1 ring-emerald-200 focus:outline-none"
    >
      {decided.message}
    </p>
  );

  return (
    <div className="max-w-5xl mx-auto px-4 py-6">
      {/* A row opened from the queue marks its entry (fromQueue), so Back returns to that queue entry and its tab. The
          mark is router state, which the Auth0 sign-in's history rewrite does not reach. A case opened on its own (a
          new tab, a pasted link) has no mark and goes to the queue's default tab. */}
      <button
        onClick={() => {
          if ((location.state as { fromQueue?: boolean } | null)?.fromQueue) navigate(-1);
          else navigate("/");
        }}
        className="text-sm text-slate-500 hover:text-slate-700 mb-4"
      >
        ← Back to queue
      </button>

      {caseQuery.isError && (
        <div
          role="status"
          className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
        >
          Could not refresh. Showing what was loaded at {new Date(caseQuery.dataUpdatedAt).toLocaleTimeString()}.{" "}
          <button type="button" className="font-medium underline" onClick={() => caseQuery.refetch()}>
            Retry
          </button>
        </div>
      )}

      <div className="flex items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Customer {caseView.customer_id}</h1>
          <div className="text-xs text-slate-500">
            Case <span className="font-mono">{caseView.case_id}</span>
            {stepName && ` · ${stepName}`}
          </div>
        </div>
        <div className="shrink-0">
          <StatusChip status={caseView.status} waitingFor={caseView.waiting_for} />
        </div>
      </div>

      {showVerdict && shown && (
        <div className="md:sticky md:top-0 z-10 mb-6 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-slate-200 bg-white/95 px-4 py-3 shadow-sm backdrop-blur">
          {label && <span className="text-xs text-slate-500">{label}</span>}
          <span
            className={`rounded-md px-3 py-1 text-lg font-semibold ring-1 ring-inset ${ACTION_TONE[shown] ?? ACTION_TONE.keep_limit}`}
          >
            {verdict}
          </span>
          {showConfidence && confidence && (
            <span
              className={
                "inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset " +
                (CONFIDENCE_STYLES[confidence] ?? "bg-slate-50 text-slate-600 ring-slate-200")
              }
            >
              {CONFIDENCE_LABELS[confidence] ?? `${confidence} confidence`}
            </span>
          )}
          {stepLine && <span className="text-sm text-slate-700">{stepLine}</span>}
          {(needsDecision || (cardRetry && mayAct)) && (
            // Scrolls without a fragment navigation: that would add a history entry with no router state, and Back to
            // queue would lose the queue tab. Focus moves to the form, as the fragment jump would move it.
            <a
              href="#decision"
              onClick={(e) => {
                e.preventDefault();
                const target = document.getElementById("decision");
                const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
                target?.scrollIntoView({ behavior: still ? "auto" : "smooth", block: "start" });
                target?.focus({ preventScroll: true });
              }}
              className="ml-auto text-sm font-medium text-brand-700 hover:underline"
            >
              Go to decision
            </a>
          )}
        </div>
      )}

      {/* What the decision did, under the strip: nothing shows here before one. An imported review's record says what
          it decided; nothing was ever sent from this system. Clear of the strip where it sticks, as #decision is. */}
      {!caseView.past_review && (
        <div id="outcome" ref={outcomeRef} className="mb-6 space-y-6 empty:hidden md:scroll-mt-32">
          {!decidedAtDocuments && decidedNotice}
          <CardOutcome caseView={caseView} />
          <NextSteps
            caseView={caseView}
            signal={{
              cash: signalRecords.cash,
              card: signalRecords.card,
              repayments: signalRecords.repayments,
              reasons: signalRecords.limit?.review_reasons,
            }}
          />
        </div>
      )}

      {caseView.outage && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 mb-4">
          <div className="flex items-start justify-between gap-4">
            <div className="text-sm text-amber-800">{caseView.outage.message}</div>
            {mayRetry && (
              <button
                type="button"
                onClick={() => retryMutation.mutate()}
                disabled={retryMutation.isPending}
                className="shrink-0 rounded-md bg-amber-700 px-3 py-1.5 text-sm font-medium text-white enabled:hover:bg-amber-800 disabled:opacity-50"
              >
                {retryMutation.isPending ? "Retrying…" : "Retry now"}
              </button>
            )}
          </div>
          {mayRetry && (
            <div className="text-xs text-amber-700 mt-1">
              {caseView.outage.kind === "data"
                ? `The ${caseView.outage.service} does not know this customer, so asking again gets the same answer. Retry only once that is fixed: if it is not, the case fails again.`
                : caseView.status === "failed"
                  ? `Retry now reopens the case and asks the ${caseView.outage.service} again, keeping what was already asked. If it is still unavailable, the case fails again.`
                  : `Retry now asks the ${caseView.outage.service} again at once, keeping what was already asked. It counts as one of the automatic retries: if the ${caseView.outage.service} is still unavailable on the last one, the case fails.`}
            </div>
          )}
          {retryMutation.isError && <ErrorBanner className="mt-2">{retryMutation.error.message}</ErrorBanner>}
        </div>
      )}

      <EngineLimitHeader
        {...signalRecords}
        cutOnTheTable={cutOnTheTable}
        // Only with a report: a case still being investigated has no recommendation to set the proposal aside.
        agentRecommendsOther={!!caseView.report && !cutOnTheTable}
      />

      {agentWorking ? (
        <div role="status" className="bg-sky-50 border border-sky-200 rounded-xl px-6 py-10 text-center">
          <div className="inline-flex items-center gap-2 text-sky-700 font-medium">
            <span aria-hidden className="inline-block h-2 w-2 rounded-full bg-sky-600 motion-safe:animate-pulse" />
            The agent is investigating.
          </div>
          <div className="text-xs text-sky-700 mt-2">This usually takes 1 to 2 minutes, and the page updates by itself.</div>
        </div>
      ) : finalising ? (
        <div role="status" className="bg-sky-50 border border-sky-200 rounded-xl px-6 py-10 text-center">
          <div className="inline-flex items-center gap-2 text-sky-700 font-medium">
            <span aria-hidden className="inline-block h-2 w-2 rounded-full bg-sky-600 motion-safe:animate-pulse" />
            Finalising the report.
          </div>
          <div className="text-xs text-sky-700 mt-2">
            The agent finished and the report is being saved. This usually takes a second or two.
          </div>
        </div>
      ) : handOverPending ? (
        <div className="h-48 rounded-xl border border-slate-200 bg-white animate-pulse" aria-busy="true" />
      ) : caseView.report ? (
        <ReportCard
          report={caseView.report}
          guidelines={caseView.guidelines}
          handedOverBy={pathQuery.data?.handed_over_by}
          drafts={pathQuery.data?.drafts}
          onEvidenceClick={(id) => setSelectedEvidence(id)}
          labelById={labelById}
        />
      ) : caseView.past_review ? (
        <PastReviewCard past={caseView.past_review} />
      ) : (
        <div className="bg-slate-50 border border-slate-200 rounded-xl p-6 text-center text-slate-500">
          No report yet.{" "}
          {/* The outage banner above names the system in the backend's words, so this line does not name it again. */}
          {caseView.status === "waiting_for_data" &&
            (caseView.outage
              ? "The investigation carries on once the problem above clears."
              : "Waiting for the bank feed or the card system to respond.")}
        </div>
      )}

      {!caseView.past_review && (
      <section className="mt-8">
        <div className="mb-3">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-lg font-semibold text-slate-900">
              {!flowKnown || fixedPlan ? "How the case was investigated" : "How the agent investigated"}
            </h2>
            {flowKnown && (
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">
                {fixedPlan ? "Fixed question plan and rule check" : "AI agent and rule check"}
              </span>
            )}
          </div>
          {flowKnown && (
            <p className="mt-1 text-sm text-slate-500">
              {fixedPlan ? "A fixed question plan picks the questions." : "The agent picks the questions."} A rule
              check looks for questions it missed and checks the report before a person sees it.
            </p>
          )}
        </div>
        {pathQuery.isLoading && !pathQuery.data && (
          <div className="text-sm text-slate-500">Loading…</div>
        )}
        {pathQuery.isError && (
          <div className="text-sm text-red-700">
            Could not load the investigation path:{" "}
            {pathQuery.error instanceof ApiError
              ? pathQuery.error.message
              : String(pathQuery.error)}
          </div>
        )}
        {pathQuery.data && (
          <InvestigationTimeline
            path={pathQuery.data}
            onEvidenceClick={(id) => setSelectedEvidence(id)}
            labelById={labelById}
          />
        )}
        {debug && pathQuery.data && (
          <button
            onClick={() => setShowRawPath((prev) => !prev)}
            className="text-xs text-slate-500 hover:text-slate-700 mt-3"
          >
            {showRawPath ? "Hide raw JSON" : "Show raw JSON (debug)"}
          </button>
        )}
        {debug && showRawPath && pathQuery.data && (
          <pre className="mt-3 bg-slate-900 text-slate-100 text-xs rounded-lg p-4 overflow-x-auto">
            {JSON.stringify(pathQuery.data, null, 2)}
          </pre>
        )}
      </section>
      )}

      {caseView.decisions && caseView.decisions.length > 0 && (
        <div className="mt-6">
          <h2 className="text-lg font-semibold text-slate-900 mb-2">Decisions</h2>
          <ul className="space-y-2">
            {caseView.decisions.map((decision, idx) => (
              <li
                key={idx}
                className="bg-white rounded-lg border border-slate-200 px-4 py-3 text-sm"
              >
                <div>
                  {decision.step && (
                    <span className="mr-2 inline-flex rounded bg-slate-100 px-1.5 py-0.5 text-xs font-medium text-slate-600">
                      {STEP_LABELS[decision.step] ?? decision.step}
                    </span>
                  )}
                  <span className="font-medium">
                    {personLabel(decision.by, decision.by_name, user?.sub)}
                    {decision.by_role && ` (${decision.by_role})`}
                  </span>{" "}
                  <span
                    className={
                      decision.decision === "approve" ? "text-emerald-700" : "text-rose-700"
                    }
                  >
                    {decision.decision === "approve"
                      ? "approved"
                      : decision.step === "documents"
                        ? "closed it without a change"
                        : "rejected"}
                  </span>
                  {decision.decided && (
                    <>
                      {" "}
                      <span className="text-slate-600">
                        with{" "}
                        <span className="font-medium">
                          {RECOMMENDATION_LABELS[decision.decided] ?? decision.decided}
                        </span>
                      </span>
                    </>
                  )}
                </div>
                {(decision.reason || decision.note) && (
                  <div className="text-slate-600 mt-1">
                    {decision.reason && (
                      <span className="font-medium text-slate-700">
                        {REASON_LABELS[decision.reason] ?? decision.reason}
                      </span>
                    )}
                    {decision.reason && decision.note && ": "}
                    {decision.note && `“${decision.note}”`}
                  </div>
                )}
                {decision.decided_at && (
                  <div className="text-xs text-slate-500 mt-1">{formatDateTime(decision.decided_at)}</div>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {caseView.documents_request && (
        <div ref={documentsRef} className="mt-6 space-y-6 md:scroll-mt-32">
          {decidedAtDocuments && decidedNotice}
          <DocumentsRequestPanel
            request={caseView.documents_request}
            // The step, not the status: after the documents step is decided, a Card retry can keep the old status.
            waiting={caseView.waiting_for === "documents"}
          />
        </div>
      )}

      {/* Next to the button the person pressed, but out of the form's block: after a 409 the re-read can move the
          case on, and the form goes away with it. Hidden once the case moves on again, say when a manager closes it
          from another browser. */}
      {decisionError && decisionError.at === caseView.waiting_for && (
        <ErrorBanner className="mt-6">{decisionError.message}</ErrorBanner>
      )}

      {(needsDecision || cardRetry) && (
        // The strip's "Go to decision" lands here, clear of the strip where it sticks.
        <div id="decision" tabIndex={-1} className="mt-6 md:scroll-mt-32 focus:outline-none">
          {caseQuery.isError && (
            <p className="text-sm text-slate-600">
              Deciding waits until the page refreshes.{" "}
              <button type="button" className="font-medium underline" onClick={() => caseQuery.refetch()}>
                Retry
              </button>
            </p>
          )}
          {/* After a failed refresh the page may be out of date, so nothing is decided from it. Hidden, not removed, so a
              rationale already typed is kept. */}
          <div hidden={caseQuery.isError}>
            {cardRetry ? (
              <CardRetryPanel
                caseView={caseView}
                onSubmit={(input) => decisionMutation.mutate(input)}
                submitting={decisionMutation.isPending}
              />
            ) : (
              <DecisionForm
                // A new form per case and step: what was picked at the step before, or on another case, must not be
                // sent at this one.
                key={`${caseView.case_id}:${caseView.decisions.length}`}
                caseView={caseView}
                onSubmit={(input) => decisionMutation.mutate(input)}
                submitting={decisionMutation.isPending}
              />
            )}
          </div>
        </div>
      )}

      <EvidenceDetailDrawer
        evidenceId={selectedEvidence}
        record={selectedEvidence ? recordById[selectedEvidence] ?? null : null}
        label={selectedEvidence ? labelById[selectedEvidence] : undefined}
        onClose={closeDrawer}
      />
    </div>
  );
}
