import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router-dom";
import { listSignalExamples, postSignal, readSignalExample } from "../api/cases";
import { useAuth } from "../auth/AuthContext";
import { ErrorBanner } from "../components/ErrorBanner";
import type { SignalAccepted } from "../types";

// The customer the demo starts from, when the list has it.
const DEMO_CUSTOMER = "c_010";

const RESULTS: Record<SignalAccepted["result"], string> = {
  opened: "A new case was opened.",
  joined: "The customer already had an open case, so the signal was added to it. No new investigation starts.",
  duplicate: "This signal was received before, so nothing changed.",
};

/** An admin sends a test customer's review signal, as the limit engine would, and opens the case it lands in. */
export default function SendSignalPage() {
  const { user } = useAuth();
  const qc = useQueryClient();
  const isAdmin = user?.role === "admin";
  const [picked, setPicked] = useState<string | null>(null);

  const examplesQuery = useQuery({
    queryKey: ["signal-examples"],
    queryFn: listSignalExamples,
    enabled: isAdmin,
  });
  const examples = examplesQuery.data ?? [];
  const customerId =
    picked ?? (examples.some((row) => row.customer_id === DEMO_CUSTOMER) ? DEMO_CUSTOMER : examples[0]?.customer_id);

  const exampleQuery = useQuery({
    queryKey: ["signal-example", customerId],
    queryFn: () => readSignalExample(customerId!),
    enabled: isAdmin && !!customerId,
  });

  const sendMutation = useMutation({
    mutationFn: postSignal,
    onSuccess: () => {
      // A new signal_id for the next send, and the queue shows the case.
      qc.invalidateQueries({ queryKey: ["signal-example"] });
      qc.invalidateQueries({ queryKey: ["cases"] });
    },
  });

  if (!user || !isAdmin) {
    return (
      <div className="max-w-5xl mx-auto px-4 py-10 text-center">
        <p className="text-slate-700">Only an admin can send a test signal.</p>
        <Link to="/" className="text-brand-700 hover:underline text-sm mt-4 inline-block">
          Back to queue
        </Link>
      </div>
    );
  }

  const example = exampleQuery.data;
  const accepted = sendMutation.data;
  // Never while a new example loads: until it does, the one on screen may be the one just sent.
  const maySend = !!example && !exampleQuery.isFetching && !exampleQuery.isError && !sendMutation.isPending;

  let body;
  if (examplesQuery.isLoading) {
    body = <div className="text-center text-slate-500 py-10">Loading the test customers…</div>;
  } else if (examplesQuery.isError) {
    body = (
      <div className="text-center py-10">
        <p className="text-red-700">Could not load the test customers.</p>
        <p className="text-slate-500 text-sm mt-1">{examplesQuery.error.message}</p>
        <button
          onClick={() => examplesQuery.refetch()}
          className="mt-4 text-sm px-3 py-1.5 bg-slate-100 hover:bg-slate-200 rounded"
        >
          Retry
        </button>
      </div>
    );
  } else {
    body = (
      <div className="bg-white rounded-xl border border-slate-200 p-4 sm:p-6 space-y-4">
        <label className="block">
          <span className="text-sm font-medium text-slate-700">Customer</span>
          <select
            value={customerId ?? ""}
            onChange={(event) => {
              setPicked(event.target.value);
              sendMutation.reset();
            }}
            className="mt-1 block w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm"
          >
            {examples.map((row) => (
              <option key={row.customer_id} value={row.customer_id}>
                {row.customer_id} {row.name}
              </option>
            ))}
          </select>
        </label>

        {customerId && !user.customers.includes(customerId) && (
          <p className="text-sm text-amber-800">
            Your account does not list {customerId}, so you will not be able to open its case. An analyst whose account
            lists it can.
          </p>
        )}

        <div>
          <div className="text-sm font-medium text-slate-700 mb-1">Signal</div>
          {exampleQuery.isError ? (
            <ErrorBanner>Could not build the signal: {exampleQuery.error.message}</ErrorBanner>
          ) : example ? (
            <pre className="max-h-96 overflow-auto rounded-md border border-slate-200 bg-slate-50 p-3 text-xs text-slate-800">
              {JSON.stringify(example, null, 2)}
            </pre>
          ) : (
            <div className="text-sm text-slate-500">Loading…</div>
          )}
        </div>

        <button
          onClick={() => example && sendMutation.mutate(example)}
          disabled={!maySend}
          className="bg-brand-600 hover:bg-brand-700 disabled:opacity-60 text-white font-medium rounded-md px-4 py-2 transition"
        >
          {sendMutation.isPending ? "Sending…" : "Send"}
        </button>

        {sendMutation.isError && <ErrorBanner>{sendMutation.error.message}</ErrorBanner>}
        {accepted && (
          <div role="status" className="rounded-lg bg-emerald-50 px-4 py-3 text-sm text-emerald-800 ring-1 ring-emerald-200">
            <p>{RESULTS[accepted.result]}</p>
            <Link to={`/cases/${accepted.case_id}`} className="mt-1 inline-block font-medium underline">
              Open case {accepted.case_id} ({accepted.status})
            </Link>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="max-w-5xl mx-auto px-4 py-6">
      <div className="mb-4">
        <h1 className="text-2xl font-semibold text-slate-900">Send a test signal</h1>
        <p className="text-sm text-slate-500 mt-1">
          A test customer's review signal, as the limit engine would send it, built from the test data with a new id
          each time.
        </p>
      </div>
      {body}
    </div>
  );
}
