import { dollarsFromCents, percentDelta } from "../lib/format";

interface LimitRecord {
  current_limit_cents?: number;
  engine_limit_cents?: number;
  cut_cents?: number;
  cut_share_bp?: number;
  is_cut?: boolean;
  very_large?: boolean;
  review_reasons?: string[];
}

interface Props {
  /** The sig_limit record from /cases/:id/path (kind: "limit_change"). */
  limit: LimitRecord | null | undefined;
}

export function EngineLimitHeader({ limit }: Props) {
  if (!limit || limit.current_limit_cents == null || limit.engine_limit_cents == null) {
    return null;
  }
  const current = limit.current_limit_cents;
  const proposed = limit.engine_limit_cents;
  const isCut = limit.is_cut ?? proposed < current;
  const delta = percentDelta(current, proposed);

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-5 mb-4">
      <div className="text-xs uppercase tracking-wide text-slate-500 mb-3">
        What the engine proposed
      </div>
      <div className="flex items-center gap-4 flex-wrap">
        <div>
          <div className="text-xs text-slate-500">Current limit</div>
          <div className="text-xl font-semibold text-slate-900">
            {dollarsFromCents(current)}
          </div>
        </div>
        <div className="text-slate-400 text-2xl font-light">→</div>
        <div>
          <div className="text-xs text-slate-500">Engine would set</div>
          <div
            className={
              "text-xl font-semibold " + (isCut ? "text-rose-700" : "text-slate-900")
            }
          >
            {dollarsFromCents(proposed)}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2">
          {isCut && (
            <span className="inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset bg-amber-50 text-amber-700 ring-amber-200">
              {delta}
            </span>
          )}
          {/*
            very_large is the policy's "the limit is very large" flag (G-12 in
            services/orchestrator/app/policy.py). It is about the size of the
            current limit, not the size of the cut: a trim on a large account
            still gets stamped. Shown regardless of direction.
          */}
          {limit.very_large && (
            <span className="inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset bg-rose-50 text-rose-700 ring-rose-200">
              large limit (G-12)
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
