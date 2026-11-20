import type { LiveState } from "../hooks/useCaseEvents";

const STYLES: Record<LiveState, string> = {
  connecting: "bg-slate-100 text-slate-600 ring-slate-200",
  live: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  offline: "bg-amber-50 text-amber-700 ring-amber-200",
};

const LABELS: Record<LiveState, string> = {
  connecting: "Connecting…",
  live: "Live",
  offline: "Live updates offline, refreshing every 5 s",
};

export function LiveStateChip({ state }: { state: LiveState }) {
  return (
    <span
      className={
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset " +
        STYLES[state]
      }
      title={LABELS[state]}
    >
      <span
        className={
          "inline-block h-1.5 w-1.5 rounded-full " +
          (state === "live"
            ? "bg-emerald-500 animate-pulse"
            : state === "offline"
            ? "bg-amber-500"
            : "bg-slate-400")
        }
      />
      {state === "live" ? "Live" : state === "offline" ? "Polling" : "Connecting"}
    </span>
  );
}
