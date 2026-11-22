import { useEffect, useRef } from "react";
import type { CasePath } from "../types";
import { dollarsFromCents, formatDate, formatDateTime, percentFromBp } from "../lib/format";

type PathRecord = CasePath["records"][number];

interface Props {
  /** The id of the record the reviewer clicked. null when nothing is open. */
  evidenceId: string | null;
  /** The resolved record, or null if the id has no matching record in /path. */
  record: PathRecord | null;
  /** The record's name on the page (recordLabels), so the heading matches the chip that opened it. */
  label?: string;
  onClose: () => void;
}

// Keys the rule in fieldLabel reads badly, by their raw name.
const FIELD_LABELS: Record<string, string> = {
  weekly_inflow_3mo_ago_cents: "Weekly inflow, 3 months ago",
  weekly_inflow_typical_cents: "Weekly inflow, usually",
  vs_compared_bp: "Now vs then",
  mask: "Last 4 digits",
  due: "Due date",
  series: "Week by week",
  week_end: "Week ending",
};

// Values the snake-case rule in renderValue reads badly. A Map: a value can be any text, "constructor" too.
const VALUE_LABELS = new Map([
  ["ach_autopay", "ACH autopay"],
  ["ach_manual", "ACH, paid by hand"],
]);

// A value of words in snake case, such as known_vendor or three_months_ago. Ids hold digits (acct_c001_a,
// txn_c_001_01), so they keep their underscores and still match the ids shown elsewhere.
const WORDS = /^[a-z]+(_[a-z]+)+$/;
const DAY = /^\d{4}-\d{2}-\d{2}$/;
const MOMENT = /^\d{4}-\d{2}-\d{2}T/;

// A field's name in words: amount_cents reads "Amount", share_of_fall_bp "Share of fall". renderValue shows the unit.
function fieldLabel(key: string): string {
  if (FIELD_LABELS[key]) return FIELD_LABELS[key];
  const words = key.replace(/_(cents|bp)$/, "").replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// A field whose name ends in _cents is money. _bp is basis points. Dates read as the rest of the page shows them.
function renderValue(key: string, value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    if (key.endsWith("_cents")) return dollarsFromCents(value, { precision: 2 });
    if (key.endsWith("_bp")) return percentFromBp(value);
    return String(value);
  }
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "string") {
    const named = VALUE_LABELS.get(value);
    if (named) return named;
    if (DAY.test(value)) return formatDate(value);
    if (MOMENT.test(value)) return formatDateTime(value);
    if (WORDS.test(value)) return value.replace(/_/g, " ");
    return value;
  }
  if (Array.isArray(value) && !value.some((item) => typeof item === "object" && item !== null)) {
    return value.length ? value.map((item) => renderValue(key, item)).join(", ") : "none";
  }
  return JSON.stringify(value);
}

// Rows of like objects, one column per key any row has. `first` heads a column of row names, for an object of objects.
function FieldTable({
  rows,
  first,
}: {
  rows: Array<{ name?: string; row: Record<string, unknown> }>;
  first?: string;
}) {
  const columns = [...new Set(rows.flatMap(({ row }) => Object.keys(row)))];
  return (
    <div className="mt-1 overflow-x-auto">
      <table className="w-full text-xs tabular-nums">
        <thead>
          <tr className="text-left text-slate-500">
            {first !== undefined && (
              <th scope="col" className="py-1 pr-3 font-medium">
                {first}
              </th>
            )}
            {columns.map((column) => (
              <th key={column} scope="col" className="py-1 pr-3 font-medium" title={column}>
                {fieldLabel(column)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map(({ name, row }, index) => (
            <tr key={name ?? index}>
              {first !== undefined && (
                <th scope="row" className="py-1 pr-3 text-left font-normal text-slate-700">
                  {name}
                </th>
              )}
              {columns.map((column) => (
                <td key={column} className="py-1 pr-3 text-slate-900">
                  {renderValue(column, row[column])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// A list of objects is a table, an object of objects (payment_summary's by_category) one too, with its keys as the
// row names. Any other object is a short list of its fields.
function FieldValue({ name, value }: { name: string; value: unknown }) {
  if (Array.isArray(value) && value.length > 0 && value.every(isObject)) {
    return <FieldTable rows={value.map((row) => ({ row }))} />;
  }
  if (isObject(value)) {
    const entries = Object.entries(value);
    if (!entries.length) return <>none</>;
    if (entries.every(([, inner]) => isObject(inner))) {
      return (
        <FieldTable
          first={fieldLabel(name.replace(/^by_/, ""))}
          rows={entries.map(([key, row]) => ({ name: fieldLabel(key), row: row as Record<string, unknown> }))}
        />
      );
    }
    return (
      <dl className="mt-1 space-y-0.5">
        {entries.map(([key, inner]) => (
          <div key={key} className="flex flex-wrap gap-x-2">
            <dt className="text-slate-500" title={key}>
              {fieldLabel(key)}
            </dt>
            <dd>
              <FieldValue name={key} value={inner} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  return <>{renderValue(name, value)}</>;
}

export function EvidenceDetailDrawer({ evidenceId, record, label, onClose }: Props) {
  const closeRef = useRef<HTMLButtonElement>(null);
  const asideRef = useRef<HTMLElement>(null);
  const open = evidenceId !== null;

  // Close on Escape so the keyboard reviewer doesn't need the mouse.
  useEffect(() => {
    if (!open) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [open, onClose]);

  // Focus moves into the drawer when it opens, and back to what opened it (the record chip) when it closes. While it is
  // open, focus stays in it: aria-modal tells a screen reader the page behind is out of reach, so Tab or Shift+Tab
  // onto the page comes back to Close. Inside the drawer Tab moves as usual, so a wide table can still be scrolled.
  useEffect(() => {
    if (!open) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    function onFocusIn(event: FocusEvent) {
      if (event.target instanceof Node && !asideRef.current?.contains(event.target)) closeRef.current?.focus();
    }
    document.addEventListener("focusin", onFocusIn);
    return () => {
      document.removeEventListener("focusin", onFocusIn);
      opener?.focus();
    };
  }, [open]);

  if (!evidenceId) return null;

  const data = record?.data ?? {};
  // A repayment's place among those listed reads as one line, "Repayment 3 of 3", not as two fields.
  const { sequence, of: total, ...rest } = data;
  const placed = typeof sequence === "number" && typeof total === "number";
  const entries = Object.entries(placed ? { position: `Repayment ${sequence} of ${total}`, ...rest } : data);

  return (
    <div className="fixed inset-0 z-50 flex justify-end" onClick={onClose}>
      <div className="absolute inset-0 bg-slate-900/40" />
      <aside
        ref={asideRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="evidence-title"
        className="relative w-full max-w-md bg-white shadow-xl h-full overflow-y-auto"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="sticky top-0 bg-white border-b border-slate-200 px-5 py-4 flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 id="evidence-title" className="text-base font-semibold text-slate-900">
              {record ? (label ?? record.kind) : "Record"}
            </h2>
            <div className="text-xs font-mono text-slate-500 break-all">{evidenceId}</div>
          </div>
          <button
            ref={closeRef}
            onClick={onClose}
            className="h-9 w-9 shrink-0 inline-flex items-center justify-center rounded-md text-xl leading-none text-slate-500 hover:bg-slate-100 hover:text-slate-900"
            aria-label="Close"
          >
            ×
          </button>
        </div>
        <div className="px-5 py-4">
          {!record ? (
            // Reports can cite records that /path does not return (e.g. `hist_`
            // or `xs_` prefixes from past-review search). The chip still opens
            // the drawer so the reviewer sees the id, with a clear note.
            <div className="text-sm text-slate-600">
              <p className="mb-2">
                No matching record in this case's investigation path.
              </p>
              <p className="text-slate-500">
                This evidence id was cited by the report but is not in the records the
                agent asked for on this run. It may be from an earlier review the agent
                looked up (<span className="font-mono">hist_*</span>) or from the case
                search (<span className="font-mono">xs_*</span>).
              </p>
            </div>
          ) : entries.length === 0 ? (
            <div className="text-sm text-slate-500 italic">No fields.</div>
          ) : (
            <dl className="divide-y divide-slate-100">
              {entries.map(([key, value]) => (
                <div key={key} className="py-2.5">
                  <dt className="text-xs font-medium text-slate-500" title={key}>
                    {fieldLabel(key)}
                  </dt>
                  <dd className="text-sm text-slate-900 mt-0.5 break-words">
                    <FieldValue name={key} value={value} />
                  </dd>
                </div>
              ))}
            </dl>
          )}
        </div>
      </aside>
    </div>
  );
}
