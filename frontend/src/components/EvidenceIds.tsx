/** The records a claim rests on, as chips like the timeline's records, so they read as something to click: each
 * opens the record. Used by the report and by each specialist's answer in the timeline. */
export function EvidenceIds({
  ids,
  onClick,
  labelById,
}: {
  ids?: string[];
  onClick?: (id: string) => void;
  labelById?: Record<string, string>;
}) {
  if (!ids?.length) return null;
  return (
    <span className="inline-flex flex-wrap items-center gap-1.5 text-xs text-slate-500">
      {ids.length === 1 ? "Source:" : "Sources:"}
      {ids.map((id) => {
        const label = labelById?.[id];
        return (
          <button
            key={id}
            type="button"
            onClick={() => onClick?.(id)}
            title={id}
            aria-label={label ? `${label}, record ${id}` : `Record ${id}`}
            className={
              "text-xs px-2 py-1 rounded bg-slate-100 text-slate-700 hover:bg-brand-50 hover:text-brand-700 transition" +
              (label ? "" : " font-mono")
            }
          >
            {label ?? id}
          </button>
        );
      })}
    </span>
  );
}
