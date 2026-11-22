import { useState } from "react";
import type { DocumentsRequest } from "../types";
import { useAuth } from "../auth/AuthContext";
import { formatDate, formatDateTime, personLabel } from "../lib/format";

interface Props {
  request: DocumentsRequest;
  /** The case still waits for the documents. Otherwise the panel is the record of what was asked. */
  waiting: boolean;
}

/** What the customer was asked for, with the message code drafted to send them. */
export function DocumentsRequestPanel({ request, waiting }: Props) {
  const { user } = useAuth();
  const [copied, setCopied] = useState<"copied" | "failed" | null>(null);
  const requestedBy = personLabel(request.requested_by, request.requested_by_name, user?.sub);

  async function copyMessage() {
    try {
      // The clipboard is there only on a secure origin (https or localhost): elsewhere this throws.
      await navigator.clipboard.writeText(request.message);
      setCopied("copied");
    } catch {
      setCopied("failed");
    }
    setTimeout(() => setCopied(null), 2_000);
  }

  return (
    <section className="mt-6">
      <h2 className="text-lg font-semibold text-slate-900 mb-2">
        {waiting ? "Waiting for the customer" : "Documents requested"}
      </h2>
      <div
        className={"bg-white rounded-xl border p-5 space-y-4 " + (waiting ? "border-sky-200" : "border-slate-200")}
      >
        {waiting && (
          <p className="text-sm text-slate-700">
            A request for documents is drafted below: send it to the customer.{" "}
            <span className="font-medium text-slate-900">The limit stays unchanged while we wait.</span> When the
            documents come in, an analyst or a manager records what they showed.
          </p>
        )}

        <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-sm">
          <div>
            <dt className="text-xs text-slate-500">Due</dt>
            <dd className="text-slate-900 font-medium">{formatDate(request.due)}</dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Requested by</dt>
            <dd className="text-slate-800">{requestedBy}</dd>
          </div>
          <div>
            <dt className="text-xs text-slate-500">Requested at</dt>
            <dd className="text-slate-800">{formatDateTime(request.requested_at)}</dd>
          </div>
        </dl>

        {request.asked_for.length > 0 && (
          <div>
            <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Asked for</div>
            <ul className="list-disc pl-5 space-y-0.5 text-sm text-slate-800">
              {request.asked_for.map((item, idx) => (
                <li key={idx}>{item}</li>
              ))}
            </ul>
          </div>
        )}

        <div>
          <div className="flex items-center justify-between gap-3 mb-1">
            <div className="text-xs uppercase tracking-wide text-slate-500">Message to the customer</div>
            <button
              type="button"
              onClick={copyMessage}
              className="text-xs px-2 py-1 border border-slate-300 rounded hover:bg-slate-50"
            >
              {copied === "copied" ? "Copied" : copied === "failed" ? "Could not copy: select the text" : "Copy"}
            </button>
          </div>
          <div className="whitespace-pre-wrap rounded-md border border-slate-200 bg-slate-50 p-3 text-sm text-slate-800">
            {request.message}
          </div>
          <p className="text-xs text-slate-500 mt-1">
            Drafted by code from the case's signal, not by the model. Nothing is sent from here: copy it into your
            message to the customer.
          </p>
        </div>
      </div>
    </section>
  );
}
