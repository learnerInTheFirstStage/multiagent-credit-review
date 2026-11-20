import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { postDecision, readCase, readPath } from "../api/cases";
import type { DecisionInput } from "../api/cases";
import { StatusChip } from "../components/StatusChip";
import { ReportCard } from "../components/ReportCard";
import { DecisionForm } from "../components/DecisionForm";
import { EngineLimitHeader } from "../components/EngineLimitHeader";
import { InvestigationTimeline } from "../components/InvestigationTimeline";
import { EvidenceDetailDrawer } from "../components/EvidenceDetailDrawer";
import { ApiError } from "../api/client";
import { useAuth } from "../auth/AuthContext";
import type { CasePath } from "../types";

const ACTIVE_STATUSES = new Set(["queued", "investigating", "waiting_for_data"]);

type PathRecord = CasePath["records"][number];

export default function CaseDetailPage() {
  const { caseId = "" } = useParams<{ caseId: string }>();
  const { user } = useAuth();
  const navigate = useNavigate();
  const qc = useQueryClient();
  const [selectedEvidence, setSelectedEvidence] = useState<string | null>(null);
  const [showRawPath, setShowRawPath] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const caseQuery = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => readCase(caseId),
    enabled: !!caseId,
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      // Poll while the case is in motion, stop once a person needs to act or it's closed.
      if (status && ACTIVE_STATUSES.has(status)) return 3_000;
      return false;
    },
  });

  // Path is always fetched: it feeds the engine-limit header, the timeline,
  // and the lookup for clickable evidence ids. Polls on the same cadence as
  // the case while the agent is working, so a new record shows up quickly.
  const caseStatus = caseQuery.data?.status;
  const pathQuery = useQuery({
    queryKey: ["case-path", caseId],
    queryFn: () => readPath(caseId),
    enabled: !!caseId,
    refetchInterval:
      caseStatus && ACTIVE_STATUSES.has(caseStatus) ? 3_000 : false,
  });

  const decisionMutation = useMutation({
    mutationFn: (input: DecisionInput) => postDecision(caseId, input),
    onSuccess: (updated) => {
      qc.setQueryData(["case", caseId], updated);
      qc.invalidateQueries({ queryKey: ["cases"] });
      setDecisionError(null);
    },
    onError: (err) => {
      setDecisionError(err instanceof ApiError ? err.message : String(err));
    },
  });

  const caseView = caseQuery.data;

  const recordById = useMemo(() => {
    const map: Record<string, PathRecord> = {};
    const records = pathQuery.data?.records ?? [];
    for (const record of records) map[record.id] = record;
    return map;
  }, [pathQuery.data]);

  const limitRecord = recordById["sig_limit"]?.data as
    | React.ComponentProps<typeof EngineLimitHeader>["limit"]
    | undefined;

  // Stable callback so the drawer's Esc listener is not re-registered on every
  // 3 s polling render.
  const closeDrawer = useCallback(() => setSelectedEvidence(null), []);

  const needsDecision =
    caseView &&
    ((caseView.status === "needs_approval" && (user?.role === "analyst" || user?.role === "manager")) ||
      (caseView.status === "needs_manager" && user?.role === "manager"));

  if (caseQuery.isLoading) {
    return <div className="text-center text-slate-500 py-10">Loading case…</div>;
  }
  if (caseQuery.isError || !caseView) {
    const err = caseQuery.error;
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
        <Link to="/" className="text-brand-600 hover:underline text-sm mt-4 inline-block">
          Back to queue
        </Link>
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto px-4 py-6">
      <button
        onClick={() => navigate("/")}
        className="text-sm text-slate-500 hover:text-slate-700 mb-4"
      >
        ← Back to queue
      </button>

      <div className="flex items-start justify-between gap-4 mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-slate-900 font-mono">{caseView.case_id}</h1>
          <div className="text-sm text-slate-500 mt-1">
            Customer <span className="font-mono">{caseView.customer_id}</span>
            {caseView.waiting_for && (
              <span className="ml-2 text-amber-600">
                • waiting for {caseView.waiting_for}
              </span>
            )}
          </div>
        </div>
        <StatusChip status={caseView.status} />
      </div>

      {caseView.outage && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 mb-4">
          <div className="text-sm text-amber-800">{caseView.outage.message}</div>
        </div>
      )}

      <EngineLimitHeader limit={limitRecord} />

      {caseView.status === "queued" || caseView.status === "investigating" ? (
        <div className="bg-sky-50 border border-sky-200 rounded-xl px-6 py-10 text-center">
          <div className="animate-pulse text-sky-700 font-medium">
            The agent is investigating…
          </div>
          <div className="text-xs text-sky-600 mt-2">
            Updates arrive every 3 seconds. A typical case takes 60 to 120 seconds.
          </div>
        </div>
      ) : caseView.report ? (
        <ReportCard
          report={caseView.report}
          onEvidenceClick={(id) => setSelectedEvidence(id)}
        />
      ) : (
        <div className="bg-slate-50 border border-slate-200 rounded-xl p-6 text-center text-slate-500">
          No report yet. {caseView.status === "waiting_for_data" && "Waiting for Bank or Card to respond."}
        </div>
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
                  <span className="font-medium">
                    {decision.by} ({decision.by_role})
                  </span>{" "}
                  <span
                    className={
                      decision.decision === "approve" ? "text-emerald-700" : "text-rose-700"
                    }
                  >
                    {decision.decision === "approve" ? "approved" : "rejected"}
                  </span>
                  {decision.decided && (
                    <>
                      {" "}
                      <span className="text-slate-600">
                        with <span className="font-medium">{decision.decided}</span>
                      </span>
                    </>
                  )}
                </div>
                {decision.note && (
                  <div className="text-slate-600 mt-1">“{decision.note}”</div>
                )}
                <div className="text-xs text-slate-400 mt-1">
                  {new Date(decision.decided_at).toLocaleString()}
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {needsDecision && (
        <div className="mt-6">
          {decisionError && (
            <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-md px-3 py-2 mb-3">
              {decisionError}
            </div>
          )}
          <DecisionForm
            caseView={caseView}
            onSubmit={(input) => decisionMutation.mutate(input)}
            submitting={decisionMutation.isPending}
          />
        </div>
      )}

      <section className="mt-8">
        <h2 className="text-lg font-semibold text-slate-900 mb-3">
          Investigation steps
        </h2>
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
            questions={pathQuery.data.questions}
            onEvidenceClick={(id) => setSelectedEvidence(id)}
          />
        )}
        {pathQuery.data && (
          <button
            onClick={() => setShowRawPath((prev) => !prev)}
            className="text-xs text-slate-500 hover:text-slate-700 mt-3"
          >
            {showRawPath ? "Hide raw JSON" : "Show raw JSON (debug)"}
          </button>
        )}
        {showRawPath && pathQuery.data && (
          <pre className="mt-3 bg-slate-900 text-slate-100 text-xs rounded-lg p-4 overflow-x-auto">
            {JSON.stringify(pathQuery.data, null, 2)}
          </pre>
        )}
      </section>

      <EvidenceDetailDrawer
        evidenceId={selectedEvidence}
        record={selectedEvidence ? recordById[selectedEvidence] ?? null : null}
        onClose={closeDrawer}
      />
    </div>
  );
}
