import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { listCases } from "../api/cases";
import { ApiError } from "../api/client";
import { ReviewReasonChip } from "../components/ReviewReasonChip";
import { StatusChip } from "../components/StatusChip";
import type { CaseRow, CaseStatus } from "../types";

// The backend's default GET /cases returns open cases only. Closed statuses
// are only returned when queried one at a time with ?status=...
const CLOSED_STATUSES: CaseStatus[] = ["done", "rejected", "failed"];

const STATUS_ORDER: CaseStatus[] = [
  "needs_approval",
  "needs_manager",
  "investigating",
  "waiting_for_data",
  "queued",
  "done",
  "rejected",
  "failed",
];

function groupByStatus(rows: CaseRow[]): Map<CaseStatus, CaseRow[]> {
  const groups = new Map<CaseStatus, CaseRow[]>();
  for (const status of STATUS_ORDER) groups.set(status, []);
  for (const row of rows) {
    const list = groups.get(row.status) ?? [];
    list.push(row);
    groups.set(row.status, list);
  }
  return groups;
}

function formatWhen(iso: string): string {
  const then = new Date(iso);
  const now = new Date();
  const seconds = Math.max(0, (now.getTime() - then.getTime()) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export default function QueuePage() {
  const navigate = useNavigate();
  const [showClosed, setShowClosed] = useState(false);

  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ["cases", { showClosed }],
    queryFn: async () => {
      const open = await listCases({ limit: 200 });
      if (!showClosed) return open;
      const closed = await Promise.all(
        CLOSED_STATUSES.map((status) => listCases({ status, limit: 100 })),
      );
      const merged: CaseRow[] = [...open.cases];
      for (const batch of closed) merged.push(...batch.cases);
      return { cases: merged, total: merged.length };
    },
    refetchInterval: 5_000, // poll for new cases + status transitions
  });

  if (isLoading) {
    return (
      <div className="text-center text-slate-500 py-10">Loading the queue…</div>
    );
  }
  if (isError) {
    const detail =
      error instanceof ApiError ? error.message : error ? String(error) : "Unknown error.";
    return (
      <div className="text-center py-10">
        <p className="text-red-700">Could not load the queue.</p>
        <p className="text-slate-500 text-sm mt-1">{detail}</p>
        <button
          onClick={() => refetch()}
          className="mt-4 text-sm px-3 py-1.5 bg-slate-100 hover:bg-slate-200 rounded"
        >
          Retry
        </button>
      </div>
    );
  }

  const cases = data?.cases ?? [];
  const groups = groupByStatus(cases);

  return (
    <div className="max-w-5xl mx-auto px-4 py-6">
      <div className="flex items-end justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900">Review queue</h1>
          <p className="text-sm text-slate-500 mt-1">
            {cases.length} case{cases.length === 1 ? "" : "s"} across the customers your token covers.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <label className="text-sm text-slate-600 flex items-center gap-1.5 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={showClosed}
              onChange={(event) => setShowClosed(event.target.checked)}
              className="h-3.5 w-3.5"
            />
            Show closed
          </label>
          <button
            onClick={() => refetch()}
            className="text-sm px-3 py-1.5 border border-slate-300 rounded hover:bg-slate-50"
          >
            Refresh
          </button>
        </div>
      </div>

      {cases.length === 0 ? (
        <div className="bg-white rounded-xl border border-slate-200 p-10 text-center text-slate-500">
          <p>No open cases right now.</p>
          <p className="text-sm mt-1">
            Post a signal via <code className="bg-slate-100 px-1 rounded">POST /signals</code> to open one.
          </p>
        </div>
      ) : (
        <div className="space-y-6">
          {STATUS_ORDER.map((status) => {
            const rows = groups.get(status) ?? [];
            if (rows.length === 0) return null;
            return (
              <section key={status}>
                <div className="flex items-center gap-2 mb-2">
                  <StatusChip status={status} />
                  <span className="text-sm text-slate-500">
                    {rows.length} case{rows.length === 1 ? "" : "s"}
                  </span>
                </div>
                <div className="bg-white rounded-xl border border-slate-200 divide-y divide-slate-100">
                  {rows.map((row) => (
                    <button
                      key={row.case_id}
                      onClick={() => navigate(`/cases/${row.case_id}`)}
                      className="w-full text-left px-4 py-3 hover:bg-slate-50 flex items-center justify-between gap-4 transition"
                    >
                      <div className="min-w-0 flex-1">
                        <div className="font-mono text-sm text-slate-900">{row.case_id}</div>
                        <div className="text-sm text-slate-500">
                          Customer <span className="font-mono">{row.customer_id}</span>
                          {row.outage && (
                            <span className="ml-2 text-amber-600">• {row.outage.message}</span>
                          )}
                        </div>
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
                      </div>
                      <div className="text-xs text-slate-400 text-right shrink-0">
                        <div>opened {formatWhen(row.created_at)}</div>
                        <div>updated {formatWhen(row.updated_at)}</div>
                      </div>
                    </button>
                  ))}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
