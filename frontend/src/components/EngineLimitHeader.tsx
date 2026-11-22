import { accountLabel, dollarsFromCents, formatDate, percentDelta, percentFromBp } from "../lib/format";
import type { ReviewReason } from "../types";
import { ReviewReasonChip } from "./ReviewReasonChip";

// The sig_ records code builds from the signal (services/orchestrator/app/graph/facts.py signal_facts). Every figure
// here is code's; the page only puts them in words.
interface LimitRecord {
  current_limit_cents?: number;
  engine_limit_cents?: number;
  cut_cents?: number;
  cut_share_bp?: number;
  is_cut?: boolean;
  very_large?: boolean;
  review_reasons?: ReviewReason[];
}

interface CashRecord {
  as_of?: string;
  cash_now_cents?: number;
  cash_7d_ago_cents?: number;
  fall_cents?: number;
  fall_share_bp?: number;
}

interface CardRecord {
  balance_cents?: number;
  used_share_bp?: number;
  over_engine_limit_cents?: number;
}

interface Repayment {
  due: string;
  amount_cents: number;
  status: string;
}

interface RepaymentsRecord {
  repayments?: Repayment[];
}

interface Account {
  id: string;
  // How a person knows the account, when the signal says: the bank's name for it and the last digits of its number.
  name?: string;
  mask?: string;
  last_synced: string;
  days_behind: number;
}

// The account as a person knows it, or its id when the signal does not name it.
function accountName(account: Account): string {
  return accountLabel(account) ?? account.id;
}

interface AccountsRecord {
  accounts?: Account[];
  any_behind?: boolean;
}

interface Props {
  /** Whether a decision on this case can set the engine's limit: the warning about a cut below the balance is said
   * only then, not on a pause or a request for documents. */
  cutOnTheTable?: boolean;
  /** Whether the agent's report recommends something other than the engine's cut: the proposal is then shown set
   * aside. False with no report, so a case still being investigated claims no recommendation. */
  agentRecommendsOther?: boolean;
  limit: LimitRecord | null | undefined;
  cash?: CashRecord;
  card?: CardRecord;
  repayments?: RepaymentsRecord;
  accounts?: AccountsRecord;
}

const STATUS_STYLES: Record<string, string> = {
  paid: "text-emerald-700",
  late: "text-amber-700",
  failed: "text-rose-700 font-medium",
};

// The fact behind each review reason, in one line, from the signal's own figures.
function reasonFact(reason: ReviewReason, props: Props): string | null {
  const { limit, cash, repayments, accounts } = props;
  if (reason === "large_cut" && limit?.current_limit_cents != null && limit.engine_limit_cents != null) {
    const fall =
      cash?.cash_7d_ago_cents != null && cash.cash_now_cents != null && (cash.fall_cents ?? 0) > 0
        ? `Linked cash fell from ${dollarsFromCents(cash.cash_7d_ago_cents)} to ${dollarsFromCents(cash.cash_now_cents)} in the week` +
          (cash.fall_share_bp != null ? ` (${percentFromBp(cash.fall_share_bp, 0)} less)` : "") +
          ", so the engine proposes "
        : "The engine proposes ";
    return (
      fall +
      `${dollarsFromCents(limit.engine_limit_cents)} instead of ${dollarsFromCents(limit.current_limit_cents)}` +
      (limit.cut_share_bp != null ? `, a ${percentFromBp(limit.cut_share_bp, 0)} cut.` : ".")
    );
  }
  if (reason === "failed_repayment") {
    const failed = (repayments?.repayments ?? []).filter((repayment) => repayment.status === "failed");
    if (!failed.length) return null;
    return failed
      .map((repayment) => `The ${dollarsFromCents(repayment.amount_cents)} repayment due ${formatDate(repayment.due)} failed.`)
      .join(" ");
  }
  if (reason === "incomplete_data") {
    const behind = (accounts?.accounts ?? []).filter((account) => account.days_behind > 0);
    if (!behind.length) return null;
    return behind
      .map(
        (account) =>
          `${accountName(account)} last reported on ${formatDate(account.last_synced)}, ${account.days_behind} days before the signal.`,
      )
      .join(" ");
  }
  if (reason === "large_limit") {
    // G-12 only when the policy's own flag says so: otherwise the report cannot cite it.
    return "The engine flags the current limit as large enough for a person to look at it" +
      (limit?.very_large ? ", as the policy does (G-12)." : ".");
  }
  return null;
}

export function EngineLimitHeader(props: Props) {
  const { limit, cash, card, repayments, accounts } = props;
  if (!limit || limit.current_limit_cents == null || limit.engine_limit_cents == null) {
    return null;
  }
  const current = limit.current_limit_cents;
  const proposed = limit.engine_limit_cents;
  const isCut = limit.is_cut ?? proposed < current;
  const delta = percentDelta(current, proposed);
  const balance = card?.balance_cents;
  const over = card?.over_engine_limit_cents ?? 0;
  const linked = accounts?.accounts ?? [];
  const behind = linked.filter((account) => account.days_behind > 0);
  const anyBehind = accounts?.any_behind ?? behind.length > 0;
  const repaymentList = repayments?.repayments ?? [];
  const anyFailed = repaymentList.some((repayment) => repayment.status === "failed");
  // The signal's figures are of its day, not today's: said so wherever the page says what the customer owes.
  const asOf = cash?.as_of ? formatDate(cash.as_of) : undefined;
  // The engine held this proposal for review. Its cut is the loudest figure only while a decision can set it; where
  // the agent recommends something else, it shows as a proposal set aside, still readable (slate-500 passes AA).
  const setAside = isCut && !!props.agentRecommendsOther;
  const proposedTone = setAside
    ? "text-slate-500 line-through decoration-slate-400"
    : isCut && props.cutOnTheTable
      ? "text-rose-700"
      : "text-slate-900";

  return (
    <div className="bg-white border border-slate-200 rounded-xl p-5 mb-4 space-y-4">
      <div>
        <div className="flex flex-wrap items-center gap-2">
          <h2 className="text-sm font-medium text-slate-700">Why this case was opened</h2>
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">Limit engine</span>
        </div>
        <p className="mt-1 mb-3 text-sm text-slate-500">
          The limit engine re-scores limits from linked bank cash. It held this proposal for review, so nothing changes
          unless a person approves.
        </p>
        <div className="flex items-center gap-4 flex-wrap">
          <div>
            <div className="text-xs text-slate-500">Current limit</div>
            <div className="text-xl font-semibold text-slate-900">
              {dollarsFromCents(current)}
              {/* The policy's "the limit is very large" flag (G-12): about the current limit, not the cut. */}
              {limit.very_large && (
                <span
                  title="Guideline G-12, Large exposure: the limit is very large, so a person decides."
                  className="ml-2 align-middle inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset bg-rose-50 text-rose-700 ring-rose-200"
                >
                  Large limit
                </span>
              )}
            </div>
          </div>
          <div className="text-slate-400 text-2xl font-light">→</div>
          <div>
            <div className="text-xs text-slate-500">Engine proposes</div>
            <div className="text-xl font-semibold">
              <span className={proposedTone}>{dollarsFromCents(proposed)}</span>
              {isCut && (
                <span
                  className={
                    "ml-2 align-middle inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset " +
                    (setAside ? "bg-slate-50 text-slate-600 ring-slate-200" : "bg-amber-50 text-amber-700 ring-amber-200")
                  }
                >
                  {delta}
                </span>
              )}
            </div>
            {setAside && (
              <div className="mt-0.5 text-xs text-slate-500">Engine proposal. The agent recommends something else.</div>
            )}
          </div>
          {balance != null && (
            <div className="sm:ml-6 sm:pl-6 sm:border-l border-slate-200">
              <div className="text-xs text-slate-500">Owed on the card{asOf ? ` on ${asOf}` : ""}</div>
              <div className="text-xl font-semibold text-slate-900">
                {dollarsFromCents(balance)}
                {card?.used_share_bp != null && (
                  <span className="ml-2 text-sm font-normal text-slate-500">
                    {percentFromBp(card.used_share_bp, 0)} of the current limit
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
        {isCut && over > 0 && props.cutOnTheTable && (
          // A charge card's balance is due in full each statement: below the balance, the cut stops new spending until
          // the customer pays down the difference.
          <div className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">
            {asOf ? `On ${asOf} the customer owed` : "The customer owed"} {dollarsFromCents(balance)},{" "}
            {dollarsFromCents(over)} more than the engine's limit. If they still owe that when the limit is cut to{" "}
            {dollarsFromCents(proposed)}, they cannot spend on the card until they pay it down by{" "}
            {dollarsFromCents(over)}: for now the cut works like a pause.
          </div>
        )}
      </div>

      {/* The signal's own reasons, each with the fact behind it, so they show even when there is no report. */}
      {!!limit.review_reasons?.length && (
        <div>
          <div className="text-xs text-slate-500 mb-2">What the engine flagged</div>
          <ul className="space-y-1.5">
            {limit.review_reasons.map((reason) => {
              const fact = reasonFact(reason, props);
              return (
                <li key={reason} className="flex items-start gap-2 text-sm text-slate-800">
                  <span className="shrink-0">
                    <ReviewReasonChip reason={reason} />
                  </span>
                  {fact && <span>{fact}</span>}
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* Each account only when one is behind: otherwise one line, not "up to date" on every case. */}
      {linked.length > 0 && !anyBehind && (
        <p className="text-sm text-slate-500">
          {linked.length === 1 ? "1 linked account, reporting" : `${linked.length} linked accounts, all reporting`}
        </p>
      )}

      {linked.length > 0 && anyBehind && (
        <div>
          <div className="text-xs text-slate-500 mb-2">Linked bank accounts</div>
          <div className="flex flex-wrap gap-2">
            {linked.map((account) => (
              <span
                key={account.id}
                className={
                  "rounded-md border px-2 py-1 text-xs " +
                  (account.days_behind > 0
                    ? "border-amber-300 bg-amber-50 text-amber-900"
                    : "border-slate-200 bg-slate-50 text-slate-700")
                }
              >
                {/* The id stays to hand, in the tooltip. */}
                <span title={account.id} className={accountLabel(account) ? "font-medium" : "font-mono"}>
                  {accountName(account)}
                </span>{" "}
                {account.days_behind > 0
                  ? `last reported ${formatDate(account.last_synced)}, ${account.days_behind} days behind`
                  : "up to date"}
              </span>
            ))}
          </div>
          {behind.length > 0 && (
            <p className="mt-2 text-sm text-amber-900">
              The cash figures leave out what {behind.map(accountName).join(" and ")} held since{" "}
              {behind.length === 1 ? "it" : "they"} stopped reporting
              {(cash?.fall_cents ?? 0) > 0 ? ", so part of the fall may be missing data rather than money leaving." : "."}
            </p>
          )}
        </div>
      )}

      {repaymentList.length > 0 && (
        <div>
          <div className="text-xs text-slate-500 mb-2">Card repayments</div>
          <table className="text-sm">
            <tbody>
              {repaymentList.map((repayment) => (
                <tr key={repayment.due}>
                  <td className="pr-4 text-slate-600">due {formatDate(repayment.due)}</td>
                  <td className="pr-4 text-slate-900 text-right">{dollarsFromCents(repayment.amount_cents)}</td>
                  <td className={STATUS_STYLES[repayment.status] ?? "text-slate-700"}>{repayment.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {anyFailed && (
            <p className="mt-1 text-xs text-slate-500">
              These are the statuses on the signal's date. Whether a failed repayment was paid since is in its
              repayment record from the card system, which the report cites.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
