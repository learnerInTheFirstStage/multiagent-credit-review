import type { ReactNode } from "react";
import type { CasePath, CasePathGap, CasePathQuestion } from "../types";
import { CAUSE_LABELS, CONFIDENCE_LABELS, RECOMMENDATION_LABELS, accountLabel, formatDate } from "../lib/format";
import { EvidenceIds } from "./EvidenceIds";

const TOPIC_LABELS: Record<string, string> = {
  transactions: "Transactions in the window",
  income: "Income trend",
  prior_year: "Prior year, same week",
  reconciliation: "Reconciliation of balances with transactions",
  accounts: "Linked accounts",
  payee_history: "Payee history",
  repayments: "Card repayments",
  paying_account: "Paying account",
  card_use: "Card use",
};

const AGENT_LABELS: Record<string, string> = {
  cash_flow: "Cash-flow specialist (bank data)",
  credit_behavior: "Credit-behavior specialist (card data)",
};

const TOOL_AGENT: Record<string, string> = {
  AskCashFlow: "cash_flow",
  AskCreditBehavior: "credit_behavior",
};

const OUTCOMES: Record<string, { label: string; style: string }> = {
  answered: { label: "answered", style: "bg-emerald-50 text-emerald-700 ring-emerald-200" },
  no_answer: { label: "no usable answer", style: "bg-amber-50 text-amber-700 ring-amber-200" },
  refused: { label: "not sent", style: "bg-rose-50 text-rose-700 ring-rose-200" },
};

// How the questions ended before a gap pass.
const ENDED_BY: Record<string, string> = {
  done: "The agent said it had enough.",
  prose: "The agent stopped asking.",
  unreadable_call: "The agent's last call could not be read, which ended the questions.",
  round_limit: "The questions stopped at the round limit.",
  plan: "The fixed plan had no more questions.",
};

const DRAFT_RESULTS: Record<string, { label: string; style: string; dot: string }> = {
  passed: { label: "passed the check", style: "bg-emerald-50 text-emerald-700 ring-emerald-200", dot: "bg-emerald-500" },
  passed_low_confidence: {
    label: "passed, low confidence",
    style: "bg-amber-50 text-amber-700 ring-amber-200",
    dot: "bg-amber-500",
  },
  refused: { label: "refused by the check", style: "bg-rose-50 text-rose-700 ring-rose-200", dot: "bg-rose-500" },
  gave_up: { label: "refused again", style: "bg-rose-50 text-rose-700 ring-rose-200", dot: "bg-rose-500" },
};

// What the report check found wrong with a draft, by its code.
const CHECK_LABELS: Record<string, string> = {
  not_a_report: "it did not fit the report shape",
  evidence_unknown: "it cited evidence no specialist returned",
  no_evidence: "it recommended an action without citing evidence",
  review_reason: "it did not answer every review reason",
  guideline_unknown: "it cited a guideline that does not exist",
  guideline_missing: "it cited no guideline",
  guideline_person: "it cited a guideline under which a person decides",
  guideline_allows: "a guideline it cited does not allow its recommendation",
  hand_over: "it handed the case over without citing the hand-over guideline alone",
  guideline_need: "a guideline it cited needs a record it did not cite, or it said none fits when one does",
  not_settled: "it said no guideline fits before that was settled",
  guideline_cover: "it passed over a failed repayment, an account behind or books that do not reconcile",
  not_a_reduction: "it recommended a cut, but the engine's limit is not one the card system can set",
  cause: "it cited no record that shows its likely cause",
  figure: "it stated figures its evidence does not show",
};

const CHIP = "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ring-inset ";

interface Props {
  path: CasePath;
  /** Click a record id to open the drawer. */
  onEvidenceClick?: (id: string) => void;
  /** Each path record's name for the page (recordLabels), so a record reads the same here as in the report. */
  labelById?: Record<string, string>;
}

function topicLabel(topic?: string): string {
  return (topic && TOPIC_LABELS[topic]) ?? topic ?? "a topic";
}

function agentLabel(agent?: string | null): string {
  return (agent && AGENT_LABELS[agent]) ?? agent ?? "No specialist";
}

const OR_LIST = new Intl.ListFormat("en", { type: "disjunction" });

// "Payroll or tax" holds an "or" of its own, so two causes with it take a comma too, as three or more do.
function causeList(causes: string[]): string {
  const labels = causes.map((cause) => CAUSE_LABELS[cause] ?? cause);
  if (labels.length === 2 && labels.some((label) => label.includes(" or "))) return labels.join(", or ");
  return OR_LIST.format(labels);
}

// Code writes each gap as "<why> Not yet asked: <tool, topic>." (needs.py). The why, without the ids of the
// signal records behind it; none when the text has no why or is not in that shape.
function gapReason(text: string | undefined): string | null {
  const cut = text?.indexOf("Not yet asked: ") ?? -1;
  if (!text || cut < 0) return null;
  return text.slice(0, cut).trim().replace(/\s*\([^()]*\)(?=\.$)/, "") || null;
}

function checkList(codes: string[]): string {
  return [...new Set(codes)].map((code) => CHECK_LABELS[code] ?? code).join("; ");
}

function plural(count: number, noun: string): string {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

function times(count: number): string {
  return count === 1 ? "once" : count === 2 ? "twice" : `${count} times`;
}

// Where a gap pass sits among the calls. One without "at" (an older case) goes after them all.
function passAt(gap: CasePathGap, calls: number): number {
  return gap.at ?? calls;
}

// Every pass but the last sent the case back; the last did too while questions came after it.
function sentBack(gaps: CasePathGap[], index: number, calls: number): boolean {
  return index < gaps.length - 1 || passAt(gaps[index], calls) < calls;
}

// The story on top, one sentence each: questions, send-backs, searches and how the report fared at the rule check.
function summarize(path: CasePath): string | null {
  const { questions, gaps, drafts, searches } = path;
  if (!questions.length && !gaps.length && !drafts.length && !searches.length) return null;
  const asker = path.flow === "fixed" || path.flow === "fixed_rules" ? "The fixed question plan" : "The agent";
  const sentences = [`${asker} asked ${plural(questions.filter((q) => q.outcome !== "refused").length, "question")}.`];
  if (path.refusals > 0) {
    sentences.push(`${plural(path.refusals, "call")} ${path.refusals === 1 ? "was" : "were"} refused before being sent.`);
  }
  for (const by of ["code", "model"] as const) {
    const back = gaps.filter((gap, i) => gap.by === by && sentBack(gaps, i, questions.length));
    if (!back.length) continue;
    // Joined with "and" alone: a topic label can hold a comma ("Prior year, same week").
    const topics = [...new Set(back.flatMap((gap) => gap.asks ?? []).map((ask) => topicLabel(ask.topic).toLowerCase()))];
    const who = by === "code" ? "The rule check" : "The agent's self-check";
    sentences.push(
      `${who} sent it back ${times(back.length)}${topics.length ? ` to ask about ${topics.join(" and ")}` : ""}.`,
    );
  }
  const searched = searches.filter((search) => search.returned).length;
  if (searched) sentences.push(`${asker} searched past cases ${times(searched)}.`);
  if (path.out_of_rounds) sentences.push("The questions stopped at the round limit.");

  const last = drafts[drafts.length - 1];
  const refused = drafts
    .filter((draft) => draft.result === "refused")
    .map((draft) => `draft ${draft.draft} (${checkList(draft.codes)})`);
  if (last?.result === "gave_up") {
    const all = drafts.length === 1 ? "the draft" : drafts.length === 2 ? "both drafts" : `all ${drafts.length} drafts`;
    sentences.push(`The rule check refused ${all}, so it handed the case to a person.`);
  } else if (last?.result === "refused") {
    sentences.push(`The rule check refused ${refused.join(", ")} and sent it back for a redraft.`);
  } else if (last) {
    const low = last.result === "passed_low_confidence" ? " with low confidence, so a person decides" : "";
    sentences.push(
      refused.length
        ? `The rule check refused ${refused.join(", ")}, then passed draft ${last.draft}${low}.`
        : `The report passed the rule check on the first draft${low}.`,
    );
  }
  return sentences.join(" ");
}

// A few ids show as chips under their label; more than that fold away behind their count.
function RecordIds({
  ids,
  verb,
  onClick,
  labelById,
}: {
  ids?: string[];
  verb: "returned" | "cited";
  onClick?: (id: string) => void;
  labelById?: Record<string, string>;
}) {
  if (!ids?.length) return null;
  const chips = (
    <div className="flex flex-wrap gap-1.5">
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
    </div>
  );
  if (ids.length <= 4) {
    return (
      <>
        <Label>Records {verb}</Label>
        {chips}
      </>
    );
  }
  return (
    <details>
      <summary className="cursor-pointer text-xs text-slate-500">
        {ids.length} records {verb}
      </summary>
      <div className="mt-1.5">{chips}</div>
    </details>
  );
}

function Label({ children }: { children: ReactNode }) {
  return <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1">{children}</div>;
}

function Step({ dot, children }: { dot: string; children: ReactNode }) {
  return (
    <li className="ml-4 relative">
      <span className={`absolute -left-[22px] top-4 flex h-3 w-3 rounded-full ring-4 ring-white ${dot}`} />
      {children}
    </li>
  );
}

function QuestionStep({
  q,
  number,
  byAgent,
  onEvidenceClick,
  labelById,
  accountNames,
}: {
  q: CasePathQuestion;
  number: number;
  byAgent: boolean;
  onEvidenceClick?: (id: string) => void;
  labelById?: Record<string, string>;
  accountNames: Record<string, string>;
}) {
  const outcome = OUTCOMES[q.outcome] ?? { label: q.outcome, style: "bg-slate-100 text-slate-600 ring-slate-200" };
  const recordId = q.record_id;
  const recordLabel = recordId ? labelById?.[recordId] : undefined;
  const separates = q.separates ?? [];
  return (
    <Step dot={q.outcome === "refused" ? "bg-rose-400" : "bg-brand-500"}>
      <div className="bg-white rounded-lg border border-slate-200 p-4 text-sm">
        <div className="flex items-center justify-between gap-2 mb-2">
          <div className="text-xs text-slate-500">{q.outcome === "refused" ? "Refused call" : `Question ${number}`}</div>
          <span className={CHIP + outcome.style}>{outcome.label}</span>
        </div>
        {q.outcome === "refused" ? (
          <div className="text-slate-700">
            {q.agent ? (
              <>
                Code did not send a call to <span className="font-medium">{agentLabel(q.agent)}</span>: it broke a rule
                of the call{byAgent && ", and the agent was told which"}.
              </>
            ) : (
              <>
                Code did not send a call to <span className="font-mono">{q.tool ?? "an unknown tool"}</span>: no
                specialist is behind it.
              </>
            )}
          </div>
        ) : (
          <>
            <div className="text-slate-900">
              <span className="font-medium">{agentLabel(q.agent)}</span> was asked about{" "}
              <span className="font-medium">{topicLabel(q.topic)}</span>
              {recordId && (
                <>
                  {recordLabel ? " for " : " for record "}
                  <button
                    type="button"
                    onClick={() => onEvidenceClick?.(recordId)}
                    title={recordId}
                    className={(recordLabel ? "font-medium" : "font-mono text-xs") + " text-brand-700 hover:underline"}
                  >
                    {recordLabel ?? recordId}
                  </button>
                </>
              )}
              {/* A linked account by its name at the bank, the id in the tooltip. An account with no name, or one
                  that is not linked and has no record, shows as its id. */}
              {q.account_id &&
                (accountNames[q.account_id] ? (
                  <>
                    {" "}for{" "}
                    <span title={q.account_id} className="font-medium">
                      {accountNames[q.account_id]}
                    </span>
                  </>
                ) : (
                  <>
                    {" "}for account <span className="font-mono text-xs">{q.account_id}</span>
                  </>
                ))}
              {q.start && q.end && (
                <>
                  {" "}from {formatDate(q.start)} to {formatDate(q.end)}
                </>
              )}
            </div>
            {separates.length > 0 && (
              <div className="mt-1 text-slate-600">Checking for: {causeList(separates)}</div>
            )}
            {q.outcome === "no_answer" && (
              <div className="mt-1 text-amber-700">{agentLabel(q.agent)} gave no usable answer. The case went on.</div>
            )}
            {/* What the specialist said, before the records it returned: the records are its sources. */}
            {q.answer && (
              <div className="mt-3">
                <Label>Answer</Label>
                <ul className="space-y-2">
                  {q.answer.findings.map((finding, index) => (
                    <li key={index} className="border-l-2 border-slate-300 pl-3">
                      <div className="text-slate-800">
                        {finding.claim}{" "}
                        <span className="text-xs text-slate-500">
                          ({(CONFIDENCE_LABELS[finding.confidence] ?? finding.confidence).toLowerCase()})
                        </span>
                      </div>
                      <div className="mt-0.5">
                        <EvidenceIds ids={finding.evidence_ids} onClick={onEvidenceClick} labelById={labelById} />
                      </div>
                    </li>
                  ))}
                </ul>
                {q.answer.could_not_determine.length > 0 && (
                  <div className="mt-2">
                    <Label>Could not tell</Label>
                    <ul className="list-disc pl-5 text-slate-700">
                      {q.answer.could_not_determine.map((item, index) => (
                        <li key={index}>{item}</li>
                      ))}
                    </ul>
                  </div>
                )}
              </div>
            )}
            {!!q.returned?.length && (
              <div className="mt-2">
                <RecordIds ids={q.returned} verb="returned" onClick={onEvidenceClick} labelById={labelById} />
              </div>
            )}
          </>
        )}
      </div>
    </Step>
  );
}

function GapStep({
  gap,
  back,
  reported,
  onEvidenceClick,
  labelById,
}: {
  gap: CasePathGap;
  back: boolean;
  reported: boolean;
  onEvidenceClick?: (id: string) => void;
  labelById?: Record<string, string>;
}) {
  const asks = gap.asks ?? [];
  const dropped = gap.dropped ?? 0;
  // The model wrote gaps, but code dropped every one for stating a figure.
  const allDropped = !gap.gaps.length && dropped > 0;
  // Code's gap texts name signal keys and tools and end with the ask; the asks below say the same plainly, each
  // with the why of its gap (a code gap's asks are in the order of its gaps).
  const asksOnly = gap.by === "code" && asks.length > 0;
  // Only a pass that sent the case back gets its asks asked. On a live case the last pass may still send it back.
  const asksLabel = back ? "Still to ask:" : reported ? "Not asked:" : "Not yet asked:";
  return (
    <Step dot="bg-amber-400">
      <div className="bg-slate-50 rounded-lg border border-dashed border-slate-300 p-4 text-sm text-slate-700 space-y-1">
        <div className="flex items-center justify-between gap-2">
          <div className="text-xs text-slate-500">Gap check</div>
          <span
            className={
              CHIP +
              (gap.gaps.length || allDropped
                ? "bg-amber-50 text-amber-700 ring-amber-200"
                : "bg-emerald-50 text-emerald-700 ring-emerald-200")
            }
          >
            {gap.gaps.length
              ? `${plural(gap.gaps.length, "gap")}, by ${gap.by === "code" ? "code" : "the model"}`
              : allDropped
                ? `${plural(dropped, "gap")} dropped`
                : "no gaps"}
          </span>
        </div>
        {gap.ended_by && ENDED_BY[gap.ended_by] && <div>{ENDED_BY[gap.ended_by]}</div>}
        {gap.by === "code" && (
          <div>Code found questions nobody asked that could still decide the case{asksOnly ? "." : ":"}</div>
        )}
        {gap.by === "model" && (
          <div>
            Code found nothing missing, so the model looked.{" "}
            {gap.gaps.length
              ? "It still saw gaps, in its own words:"
              : allDropped
                ? `It wrote ${plural(dropped, "gap")}, but ${dropped === 1 ? "it" : "each"} stated a figure, so code dropped ${dropped === 1 ? "it" : "them"}.`
                : "It saw no gaps."}
          </div>
        )}
        {gap.by === "none" && <div>Code found nothing missing.</div>}
        {gap.gaps.length > 0 && !asksOnly && (
          <ul className="list-disc ml-5 space-y-0.5">
            {gap.gaps.map((text, idx) => (
              <li key={idx} className={gap.by === "model" ? "italic" : undefined}>
                {text}
              </li>
            ))}
          </ul>
        )}
        {dropped > 0 && !allDropped && (
          <div className="text-xs text-slate-500">
            Code dropped {plural(dropped, "more gap")} the model wrote, because {dropped === 1 ? "it" : "they"} stated a
            figure.
          </div>
        )}
        {asks.length > 0 && (
          <div>
            {asksLabel}
            <ul className="list-disc ml-5 space-y-0.5">
              {asks.map((ask, idx) => {
                const reason = asksOnly ? gapReason(gap.gaps[idx]) : null;
                const recordId = ask.record_id;
                const recordLabel = recordId ? labelById?.[recordId] : undefined;
                return (
                  <li key={idx}>
                    {agentLabel(TOOL_AGENT[ask.tool] ?? ask.tool)} on {topicLabel(ask.topic).toLowerCase()}
                    {recordId && (
                      <>
                        {recordLabel ? " for " : " for record "}
                        <button
                          type="button"
                          onClick={() => onEvidenceClick?.(recordId)}
                          title={recordId}
                          className={
                            (recordLabel ? "font-medium" : "font-mono text-xs") + " text-brand-700 hover:underline"
                          }
                        >
                          {recordLabel ?? recordId}
                        </button>
                      </>
                    )}
                    {reason && <div className="text-xs text-slate-500">Why: {reason}</div>}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
        {back ? (
          <div className="font-medium text-slate-900">Sent the case back for more questions.</div>
        ) : (
          reported && (
            <div className="font-medium text-slate-900">
              Went on to the report
              {asksOnly
                ? ", which was told these questions are still open."
                : gap.by === "code" && gap.gaps.length > 0
                  ? ", which was told these are still unexplained."
                  : "."}
            </div>
          )
        )}
      </div>
    </Step>
  );
}

export function InvestigationTimeline({ path, onEvidenceClick, labelById }: Props) {
  const summary = summarize(path);
  if (!summary) {
    return <div className="text-sm text-slate-500 italic">No investigation steps recorded yet.</div>;
  }
  const { questions, gaps, drafts, searches } = path;
  const byAgent = path.flow !== "fixed" && path.flow !== "fixed_rules";
  // Each linked account the path returned with a name, as its bank names it. Not recordLabels: an unnamed account
  // there reads "Linked account", which says less than its id.
  const accountNames: Record<string, string> = {};
  for (const record of path.records ?? []) {
    const name = record.kind === "linked_account" ? accountLabel(record.data) : null;
    if (name) accountNames[record.id] = name;
  }

  // The calls in order, each gap pass before the call that came after it.
  const steps: ReactNode[] = [];
  let pass = 0;
  let number = 0;
  const addPass = () => {
    const index = pass++;
    steps.push(
      <GapStep
        key={`gap-${index}`}
        gap={gaps[index]}
        back={sentBack(gaps, index, questions.length)}
        reported={drafts.length > 0}
        onEvidenceClick={onEvidenceClick}
        labelById={labelById}
      />,
    );
  };
  questions.forEach((q, i) => {
    while (pass < gaps.length && passAt(gaps[pass], questions.length) <= i) addPass();
    if (q.outcome !== "refused") number++;
    steps.push(
      <QuestionStep
        key={`q-${i}`}
        q={q}
        number={number}
        byAgent={byAgent}
        onEvidenceClick={onEvidenceClick}
        labelById={labelById}
        accountNames={accountNames}
      />,
    );
  });
  while (pass < gaps.length) addPass();

  // The story line always shows; the steps open from it. Not controlled: the path refetches while the case runs.
  return (
    <details>
      <summary className="cursor-pointer text-sm font-medium text-slate-900 bg-white border border-slate-200 rounded-lg px-4 py-3">
        {summary}
      </summary>
      <ol className="relative border-l border-slate-200 ml-2 mt-4 space-y-4">
        {steps}

        {drafts.map((draft) => {
          const result = DRAFT_RESULTS[draft.result] ?? {
            label: draft.result,
            style: "bg-slate-100 text-slate-600 ring-slate-200",
            dot: "bg-slate-400",
          };
          return (
            <Step key={`draft-${draft.draft}`} dot={result.dot}>
              <div className="bg-white rounded-lg border border-slate-200 p-4 text-sm text-slate-700 space-y-1">
                <div className="flex items-center justify-between gap-2">
                  <div className="text-xs text-slate-500">Report draft {draft.draft}</div>
                  <span className={CHIP + result.style}>{result.label}</span>
                </div>
                {draft.recommendation && (
                  <div className="text-slate-900">
                    Recommended{" "}
                    <span className="font-medium">
                      {RECOMMENDATION_LABELS[draft.recommendation] ?? draft.recommendation}
                    </span>
                    {draft.confidence && <>, {draft.confidence} confidence</>}
                    {draft.likely_cause && (
                      <>
                        , likely cause{" "}
                        <span className="font-medium">{CAUSE_LABELS[draft.likely_cause] ?? draft.likely_cause}</span>
                      </>
                    )}
                    {draft.guideline_ids.length > 0 && (
                      <>
                        , citing <span className="font-mono text-xs">{draft.guideline_ids.join(", ")}</span>
                      </>
                    )}
                  </div>
                )}
                {draft.codes.length > 0 && <div className="text-rose-700">The check found {checkList(draft.codes)}.</div>}
                {draft.result === "refused" && <div>Code sent it back for a redraft.</div>}
                {draft.result === "gave_up" && <div>No redraft left: code handed the case to a person.</div>}
                {draft.result === "passed_low_confidence" && <div>Its confidence was low, so a person decides.</div>}
                {draft.cited.length > 0 && (
                  <div className="pt-1">
                    <RecordIds ids={draft.cited} verb="cited" onClick={onEvidenceClick} labelById={labelById} />
                  </div>
                )}
              </div>
            </Step>
          );
        })}
      </ol>

      {/* Not in the list: the path does not say where among the questions a search was made. */}
      {searches.length > 0 && (
        <div className="mt-4 bg-white rounded-lg border border-slate-200 p-4 text-sm text-slate-700">
          <div className="text-xs text-slate-500 mb-1">Past-case searches, made during the questions</div>
          <p className="text-xs text-slate-500 mb-2">
            Closed reviews like this one, as background. A report cannot cite them, and they can be other customers'
            reviews, so they are not linked here. The agent words each search itself and nothing checks those words
            for figures, so they are not shown.
          </p>
          <ul className="space-y-1">
            {searches.map((search, idx) =>
              search.returned ? (
                <li key={idx}>
                  A search found {search.returned.length ? plural(search.returned.length, "closed review") : "none"}.
                </li>
              ) : (
                <li key={idx} className="text-rose-700">
                  A search was not served, or it failed.
                </li>
              ),
            )}
          </ul>
        </div>
      )}
    </details>
  );
}
