import { useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { listCases, readCase } from "../api/cases";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import { ReviewReasonChip } from "../components/ReviewReasonChip";
import { StatusChip } from "../components/StatusChip";
import {
  ACTION_TONE,
  CONFIDENCE_STYLES,
  RECOMMENDATION_LABELS,
  STEP_LABELS,
  dollarsFromCents,
  formatDate,
  percentFromBp,
  personLabel,
} from "../lib/format";
import type { CaseRow, CaseStatus } from "../types";

type Tab = "mine" | "open" | "closed";

// Whose move a row is, for the viewer. yours: the case is in their "Waiting on me".
type Move = { yours: boolean; text: string };
// The outcome a person picked, which the manager step confirms.
type Picked = { title: string; choice: string; label: string };

// The backend's default GET /cases returns open cases only. Closed statuses
// are only returned when queried one at a time with ?status=...
const CLOSED_STATUSES: CaseStatus[] = ["done", "rejected", "failed"];

// A case the card system has not carried out keeps the status of the step that approved it (cases.py decide), so it is
// grouped on its own: under that status, the group's chip would name a step that is already over.
type Group = CaseStatus | "retry";

const STATUS_ORDER: Group[] = [
  "retry",
  "needs_approval",
  "needs_manager",
  "awaiting_documents",
  "investigating",
  "waiting_for_data",
  "queued",
  "done",
  "rejected",
  "failed",
];

// The steps each role decides, in the order "Waiting on me" shows them. A view filter: the backend still says who
// may act, and refuses anyone else.
const MY_STEPS: Partial<Record<string, CaseStatus[]>> = {
  analyst: ["needs_approval", "awaiting_documents"],
  manager: ["needs_manager", "needs_approval", "awaiting_documents"],
};

// A case in one of these waits for a person, so the time since it last changed is how long it has waited.
const WAITING_STATUSES = new Set<CaseStatus>(["needs_approval", "needs_manager", "awaiting_documents"]);

const TAB_INTROS: Record<Tab, string> = {
  mine: "Cases where you make the next decision, longest waiting first.",
  open: "Every open case across the customers your token covers.",
  closed: "Done, rejected and failed cases.",
};

function groupRows(rows: CaseRow[], order: Group[], groupOf: (row: CaseRow) => Group): Array<[Group, CaseRow[]]> {
  return order
    .map((group): [Group, CaseRow[]] => [group, rows.filter((row) => groupOf(row) === group)])
    .filter(([, list]) => list.length > 0);
}

function since(iso: string): string {
  const seconds = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return `${Math.floor(seconds / 86400)}d`;
}

// When the customer's documents fall due, as the analyst needs it: overdue first, since that case needs a decision.
function documentsDue(due: string): { text: string; overdue: boolean } {
  const today = new Date().toISOString().slice(0, 10);
  if (due >= today) return { text: `Documents due ${formatDate(due)}`, overdue: false };
  const days = Math.round((Date.parse(today) - Date.parse(due)) / 86_400_000);
  return { text: `Documents overdue ${days} day${days === 1 ? "" : "s"} (due ${formatDate(due)})`, overdue: true };
}

// showMove: false on "Waiting on me", where every row is the reader's move and the tab already says so.
function QueueRow({
  row,
  move,
  picked,
  showMove,
}: {
  row: CaseRow;
  move: Move | null;
  picked: Picked | null;
  showMove: boolean;
}) {
  const step = row.waiting_for ? STEP_LABELS[row.waiting_for] : null;
  const changed = WAITING_STATUSES.has(row.status)
    ? `waiting ${since(row.updated_at)}`
    : CLOSED_STATUSES.includes(row.status)
      ? `closed ${since(row.updated_at)} ago`
      : `updated ${since(row.updated_at)} ago`;
  const opened = since(row.created_at);
  // A hand-over has no recommendation to show. Its confidence is left out too: when code gave up it is a
  // placeholder, and the row cannot tell that hand-over from the others.
  const handedOver = row.recommendation === "needs_human";
  const hasReport = !!row.recommendation || !!row.confidence || row.cut_share_bp != null;
  return (
    // fromQueue tells the case page's Back that the entry before is this queue tab.
    <Link
      to={`/cases/${row.case_id}`}
      state={{ fromQueue: true }}
      className="block w-full px-4 py-3 hover:bg-slate-50 transition"
    >
      <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between sm:gap-4">
        <div className="min-w-0 flex-1">
          <div className="text-sm font-medium text-slate-900">
            Customer {row.customer_id}
            {row.outage && <span className="ml-2 font-normal text-amber-700">• {row.outage.message}</span>}
          </div>
          <div className="font-mono text-xs text-slate-500">{row.case_id}</div>
          {row.imported && (
            <div className="mt-1 text-sm text-slate-600">Past review imported from earlier records, not investigated here</div>
          )}
          {row.documents_due && (
            <div
              className={
                "mt-1 text-sm " + (documentsDue(row.documents_due).overdue ? "font-medium text-rose-700" : "text-slate-600")
              }
            >
              {documentsDue(row.documents_due).text}
            </div>
          )}
          {/* The `&&` guard is defensive: if a browser on the new
              frontend talks to an older orchestrator during a rolling
              deploy, signal_reasons may be missing from the response. */}
          {row.signal_reasons && row.signal_reasons.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {row.signal_reasons.map((reason) => (
                <ReviewReasonChip key={reason} reason={reason} />
              ))}
            </div>
          )}
          {hasReport && (
            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
              {picked ? (
                <span className="inline-flex items-center gap-1.5 text-slate-500">
                  {picked.title}
                  <span
                    className={`rounded-md px-2 py-0.5 text-xs font-semibold ring-1 ring-inset ${
                      ACTION_TONE[picked.choice] ?? ACTION_TONE.keep_limit
                    }`}
                  >
                    {picked.label}
                  </span>
                </span>
              ) : handedOver ? (
                <span
                  className={`rounded-md px-2 py-0.5 text-xs font-semibold ring-1 ring-inset ${ACTION_TONE.needs_human}`}
                >
                  {/* A status, as the other rows' "Agent recommends": whose move it is shows beside it. */}
                  Handed over: no recommendation
                </span>
              ) : (
                row.recommendation && (
                  <span className="inline-flex items-center gap-1.5 text-slate-500">
                    Agent recommends
                    <span
                      className={`rounded-md px-2 py-0.5 text-xs font-semibold ring-1 ring-inset ${
                        ACTION_TONE[row.recommendation] ?? ACTION_TONE.keep_limit
                      }`}
                    >
                      {RECOMMENDATION_LABELS[row.recommendation] ?? row.recommendation}
                    </span>
                  </span>
                )
              )}
              {/* The agent's confidence is in its own recommendation, not in a person's pick. */}
              {row.confidence && !handedOver && !picked && (
                <span
                  className={
                    "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset " +
                    (CONFIDENCE_STYLES[row.confidence] ?? "bg-slate-50 text-slate-600 ring-slate-200")
                  }
                >
                  {row.confidence} confidence
                </span>
              )}
              {/* Every case here is an engine proposal held for review: nothing is cut unless a person approves. */}
              {row.cut_share_bp != null && (
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-700">
                  Engine proposes a {percentFromBp(row.cut_share_bp, 0)} cut
                </span>
              )}
            </div>
          )}
        </div>
        <div className="text-xs text-slate-500 break-words sm:max-w-[16rem] sm:shrink-0 sm:text-right">
          {move && showMove && (
            <div className={"text-sm font-medium " + (move.yours ? "text-brand-700" : "text-slate-700")}>
              {move.text}
            </div>
          )}
          {step && <div className="text-sm text-slate-600">{step}</div>}
          {/* An import's times are when it was loaded, not when the review happened: its page has the date. */}
          {row.imported ? (
            <div>imported</div>
          ) : (
            <>
              <div>{changed}</div>
              {/* Said only when it adds something: a case that has not changed since it opened reads the same twice. */}
              {opened !== since(row.updated_at) && <div>opened {opened} ago</div>}
            </>
          )}
        </div>
      </div>
    </Link>
  );
}

export default function QueuePage() {
  const qc = useQueryClient();
  const { user } = useAuth();
  const mySteps = user ? MY_STEPS[user.role] : undefined;
  // The tab lives in the URL, so Back from a case lands on the tab it was opened from. A tab the reader does not have,
  // or one this page does not know, falls back to the default.
  const [params, setParams] = useSearchParams();
  const asked = params.get("tab");
  const tab: Tab =
    asked === "open" || asked === "closed" || (asked === "mine" && mySteps) ? asked : mySteps ? "mine" : "open";

  // "Waiting on me" and "All open" read this one list. Every key here starts with "cases", so the event stream's
  // invalidate reaches it.
  const openQuery = useQuery({
    queryKey: ["cases", "open"],
    queryFn: () => listCases({ limit: 200 }),
    refetchInterval: 5_000, // poll for new cases + status transitions
  });
  const closedQuery = useQuery({
    queryKey: ["cases", "closed"],
    queryFn: async () => {
      const batches = await Promise.all(CLOSED_STATUSES.map((status) => listCases({ status, limit: 100 })));
      return batches.flatMap((batch) => batch.cases);
    },
    // Read on every tab, so the Closed count shows before the tab is opened; slower while it is not the one on screen.
    refetchInterval: tab === "closed" ? 5_000 : 30_000,
  });

  const openRows = openQuery.data?.cases ?? [];
  // A queue row does not say who approved last, so the app reads the cases where that decides whose move it is: the
  // manager step for a manager (it needs someone other than the approver of the step before) and a Card retry (only
  // that approver may send it again). Keyed by updated_at and waiting_for, so a case that moves on is read again: a
  // Card failure changes waiting_for but not updated_at. Under "cases", so Refresh and the event stream re-read them.
  const toRead = mySteps
    ? openRows.filter(
        (row) => row.waiting_for === "retry" || (user?.role === "manager" && row.status === "needs_manager"),
      )
    : [];
  const reads = useQueries({
    queries: toRead.map((row) => ({
      queryKey: ["cases", "approver", row.case_id, row.updated_at, row.waiting_for ?? null],
      queryFn: () => readCase(row.case_id),
    })),
  });
  const readById = new Map(reads.flatMap(({ data }) => (data ? [[data.case_id, data] as const] : [])));
  // The read also knows waiting_for when the row comes from a backend that does not send it.
  const waitingOf = (row: CaseRow) => readById.get(row.case_id)?.waiting_for ?? row.waiting_for;

  // Whose move the row is, by the backend's rules (cases.py decide).
  function moveOf(row: CaseRow): Move | null {
    if (!user || !mySteps?.includes(row.status)) return null;
    const read = readById.get(row.case_id);
    const last = read?.decisions[read.decisions.length - 1];
    const waiting = waitingOf(row);
    if (waiting === "retry") {
      // Card refused it: sending it again gets the same answer (the case page offers no resend), so only a manager's
      // close is left.
      if (read?.card_refusal) {
        if (user.role === "manager") return { yours: true, text: "Your move" };
        return { yours: false, text: "The card system refused it. Waiting for a manager to close it" };
      }
      // The approver may send it again and a manager may close it. Anyone else waits, also while the read is
      // pending: unlike the manager step, a retry is rarely the viewer's.
      if (last?.by === user.sub || user.role === "manager") return { yours: true, text: "Your move" };
      return last
        ? {
            yours: false,
            text: `Waiting for ${personLabel(last.by, last.by_name, user.sub)} to send it to the card system again`,
          }
        : null;
    }
    // The manager step needs another person. Kept as the viewer's while the read is pending or failed.
    if (waiting === "manager" && last?.by === user.sub) {
      // As the decision form says it: someone else confirms it. Never "another manager": a manager's approval is final.
      return { yours: false, text: "You approved the step before. Someone else must confirm it" };
    }
    return { yours: true, text: "Your move" };
  }
  // After a hand-over or the documents step the manager confirms what a person picked, not the agent's recommendation,
  // and a Card failure is about that pick, as the case page's strip says. Known where the case was read: a manager's
  // own step and a Card failure. After a Card failure the pick can sit behind the manager's sign-off.
  function pickedOf(row: CaseRow): Picked | null {
    const read = readById.get(row.case_id);
    if (!read) return null;
    const last = read.decisions[read.decisions.length - 1];
    const pick =
      read.waiting_for === "manager"
        ? last?.decided
          ? last
          : undefined
        : read.waiting_for === "retry"
          ? [...read.decisions].reverse().find((decision) => !!decision.decided)
          : undefined;
    if (!pick?.decided) return null;
    const choice = Object.keys(read.approval_outcomes ?? {})[0] ?? pick.decided;
    const cut = read.approval_outcomes?.[choice]?.action;
    return {
      title: pick.step === "documents" ? "What the documents showed" : "Picked at the analyst step",
      choice,
      label:
        cut?.type === "reduce_limit"
          ? `Reduce limit to ${dollarsFromCents(cut.limit_cents)}`
          : (RECOMMENDATION_LABELS[choice] ?? choice),
    };
  }
  const mine = openRows
    .filter((row) => moveOf(row)?.yours)
    .sort((a, b) => Date.parse(a.updated_at) - Date.parse(b.updated_at));

  const tabs: Array<{ id: Tab; label: string; count?: number }> = [
    { id: "open", label: "All open", count: openQuery.data?.cases.length },
    { id: "closed", label: "Closed", count: closedQuery.data?.length },
  ];
  if (mySteps) tabs.unshift({ id: "mine", label: "Waiting on me", count: openQuery.data ? mine.length : undefined });
  const active = tab === "closed" ? closedQuery : openQuery;
  const rows = tab === "mine" ? mine : tab === "open" ? openRows : (closedQuery.data ?? []);
  // "Waiting on me" keeps the role's step order, after the card system's problems. A separate list: moveOf reads
  // MY_STEPS by the real status.
  const groups = groupRows(rows, tab === "mine" && mySteps ? ["retry", ...mySteps] : STATUS_ORDER, (row) =>
    waitingOf(row) === "retry" ? "retry" : row.status,
  );

  let body;
  if (active.isLoading) {
    body = <div className="text-center text-slate-500 py-10">Loading the queue…</div>;
  } else if (active.isError && !active.data) {
    // Only when nothing was ever loaded: a failed poll keeps the list on screen, with the banner above it.
    const error = active.error;
    const detail = error instanceof ApiError ? error.message : error ? String(error) : "Unknown error.";
    body = (
      <div className="text-center py-10">
        <p className="text-red-700">Could not load the queue.</p>
        <p className="text-slate-500 text-sm mt-1">{detail}</p>
        <button
          onClick={() => active.refetch()}
          className="mt-4 text-sm px-3 py-1.5 bg-slate-100 hover:bg-slate-200 rounded"
        >
          Retry
        </button>
      </div>
    );
  } else if (rows.length === 0) {
    body = (
      <div className="bg-white rounded-xl border border-slate-200 p-10 text-center text-slate-500">
        {tab === "mine" ? (
          <>
            <p>Nothing waits on you right now.</p>
            <p className="text-sm mt-1">A case shows up here when it needs your decision.</p>
          </>
        ) : tab === "open" ? (
          <>
            <p>No open cases.</p>
            <p className="text-sm mt-1">A case opens when the limit engine holds one of its proposals for a person to review.</p>
          </>
        ) : (
          <p>No closed cases yet.</p>
        )}
      </div>
    );
  } else {
    body = (
      <div className="space-y-6">
        {groups.map(([group, list]) => (
          <section key={group}>
            <div className="flex items-center gap-2 mb-2">
              {/* A retry group's rows may hold different statuses: the chip shows the retry, whatever the status. */}
              <StatusChip
                status={group === "retry" ? list[0].status : group}
                waitingFor={group === "retry" ? "retry" : null}
              />
              <span className="text-sm text-slate-500">
                {list.length} case{list.length === 1 ? "" : "s"}
              </span>
            </div>
            <div className="bg-white rounded-xl border border-slate-200 divide-y divide-slate-100">
              {list.map((row) => (
                <QueueRow
                  key={row.case_id}
                  row={row}
                  move={moveOf(row)}
                  picked={pickedOf(row)}
                  showMove={tab !== "mine"}
                />
              ))}
            </div>
          </section>
        ))}
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto px-4 py-6">
      <div className="mb-4 flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Review queue</h1>
          <p className="text-sm text-slate-500 mt-1">{TAB_INTROS[tab]}</p>
        </div>
        <button
          onClick={() => qc.invalidateQueries({ queryKey: ["cases"] })}
          className="self-start sm:self-auto text-sm px-3 py-1.5 border border-slate-300 rounded hover:bg-slate-50"
        >
          Refresh
        </button>
      </div>

      <div className="flex gap-1 border-b border-slate-200 mb-6">
        {tabs.map(({ id, label, count }) => (
          <button
            key={id}
            onClick={() => setParams({ tab: id }, { replace: true })}
            className={
              "px-2 sm:px-3 py-2 -mb-px border-b-2 text-sm transition " +
              (tab === id
                ? "border-brand-600 text-brand-700 font-medium"
                : "border-transparent text-slate-500 hover:text-slate-700")
            }
          >
            {label}
            {count !== undefined && (
              <span className="ml-1.5 rounded-full bg-slate-100 px-1.5 py-0.5 text-xs text-slate-600">{count}</span>
            )}
          </button>
        ))}
      </div>

      {active.isError && active.data && (
        <div
          role="status"
          className="mb-4 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900"
        >
          Could not refresh. Showing what was loaded at {new Date(active.dataUpdatedAt).toLocaleTimeString()}.{" "}
          <button type="button" className="font-medium underline" onClick={() => active.refetch()}>
            Retry
          </button>
        </div>
      )}

      {body}
    </div>
  );
}
