import type { CasePath, Guideline, Report, ReviewReason } from "../types";
import {
  ACTION_TONE,
  CAUSE_LABELS,
  CONFIDENCE_LABELS,
  CONFIDENCE_STYLES,
  RECOMMENDATION_LABELS,
  reportAuthor,
  showsConfidence,
  undeterminedHandOver,
} from "../lib/format";
import { EvidenceIds } from "./EvidenceIds";
import { ReviewReasonChip } from "./ReviewReasonChip";

interface Props {
  report: Report;
  /** The guidelines the report cites, with their wording. Undefined from a backend that does not send them. */
  guidelines?: Guideline[];
  /** Who handed a needs_human report over, from the case path. Undefined until the path loads. */
  handedOverBy?: CasePath["handed_over_by"];
  /** The checked drafts from the case path, for the hand-over banner. */
  drafts?: CasePath["drafts"];
  /** Called when the reviewer clicks on an evidence id. */
  onEvidenceClick?: (id: string) => void;
  /** Each path record's name for the page (recordLabels), so a citation names the record. Empty until the path loads. */
  labelById?: Record<string, string>;
}

// The evidence list's markup, shared by the review reasons, the evidence and what else was found.
function ClaimList({
  title,
  items,
  onEvidenceClick,
  labelById,
}: {
  title: string;
  items?: Array<{ text?: string; evidence_ids?: string[]; reason?: ReviewReason }>;
  onEvidenceClick?: (id: string) => void;
  labelById?: Record<string, string>;
}) {
  if (!items?.length) return null;
  return (
    <div className="mb-4">
      <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">{title}</div>
      <ul className="space-y-2">
        {items.map((item, idx) => (
          <li key={idx} className="border-l-2 border-brand-500 pl-3">
            {item.reason && (
              <div className="mb-1">
                <ReviewReasonChip reason={item.reason} />
              </div>
            )}
            {item.text && <div className="text-slate-800">{item.text}</div>}
            <div className="mt-0.5">
              <EvidenceIds ids={item.evidence_ids} onClick={onEvidenceClick} labelById={labelById} />
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function ReportCard({ report, guidelines, handedOverBy, drafts = [], onEvidenceClick, labelById }: Props) {
  const likelyCause = report.likely_cause as string | undefined;
  const confidence = report.confidence as string | undefined;
  const recommendation = report.recommendation as string | undefined;
  // A hand-over for a cause nobody settled: the cause line says so and no recommendation is shown. showsConfidence
  // decides the confidence.
  const hideRecommendation = undeterminedHandOver(likelyCause, recommendation);
  // When code gave up it wrote a placeholder cause and confidence, not findings, so neither is shown.
  const gaveUp = handedOverBy === "gave_up";
  // On a low-confidence hand-over code replaced the recommendation, so what the agent wanted is in the last draft.
  const draftRecommendation = drafts[drafts.length - 1]?.recommendation;
  const failedDrafts = drafts.filter((draft) => draft.result === "refused" || draft.result === "gave_up");
  // A backend that does not send the guidelines' wording still has their ids.
  const allCited: Array<Partial<Guideline> & { id: string }> =
    guidelines ?? report.guideline_ids?.map((id) => ({ id })) ?? [];
  // A report can cite an id twice: show it once.
  const cited = allCited.filter(
    (guideline, index) => allCited.findIndex((other) => other.id === guideline.id) === index,
  );
  // The cited guideline that hands the case to a person, as in "Records disagree", shown after "Cause not determined".
  // Only one that allows needs_human: a low-confidence hand-over keeps the acting draft's guidelines, which are not why
  // a person decides. G-11 only says the cause cannot be determined, as the cause line already does.
  const openBecause = cited.find(
    (guideline) => guideline.title && guideline.id !== "G-11" && guideline.allows?.includes("needs_human"),
  )?.title;

  // Only a report that passed the rule check is the agent's: when code gave up, code wrote this one. On a hand-over,
  // which of the two it is comes from the path, and while that is unknown the card claims neither, nor a confidence
  // that may be code's placeholder.
  const author = reportAuthor(recommendation, handedOverBy);
  const checkBadge =
    author === "code"
      ? "Written by code · the drafts failed the rule check"
      : author === "agent"
        ? "AI agent · passed the rule check"
        : null;
  // The hand-over banner already says no recommendation was made, so the chip shows only where no banner does: a
  // hand-over whose path did not load.
  const showRecommendation = !hideRecommendation && !!recommendation && !handedOverBy;
  const showConfidence = !!confidence && showsConfidence(recommendation, likelyCause, handedOverBy);

  return (
    <div className="bg-white rounded-xl border border-slate-200 p-6">
      <div className="flex flex-wrap items-center gap-2 mb-4">
        <h2 className="text-lg font-semibold text-slate-900">
          {author === "agent" ? "The agent's report" : "The report"}
        </h2>
        {checkBadge && (
          <span className="rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-600">{checkBadge}</span>
        )}
      </div>

      {handedOverBy && (
        <div className="mb-4 bg-amber-50 border border-amber-200 rounded-lg px-4 py-3 text-sm text-amber-800">
          <div className="font-semibold text-amber-900">No recommendation: handed over to a person</div>
          {handedOverBy === "report" && (
            <p className="mt-1">
              The agent handed it over itself{hideRecommendation ? ": it could not settle the cause." : "."}
            </p>
          )}
          {handedOverBy === "low_confidence" && (
            <p className="mt-1">
              The rule check handed it over because the agent's confidence was low.
              {draftRecommendation && (
                <>
                  {" "}The agent's draft recommended{" "}
                  <span className="font-medium">
                    {RECOMMENDATION_LABELS[draftRecommendation] ?? draftRecommendation}
                  </span>
                  .
                </>
              )}
            </p>
          )}
          {gaveUp && (
            <>
              <p className="mt-1">
                The rule check handed it over after {failedDrafts.length} drafts failed.
                {!!report.other_findings?.length && " What the specialists found is listed under Also found."}
              </p>
              <ul className="mt-1 space-y-0.5">
                {failedDrafts.map((draft) => (
                  <li key={draft.draft}>
                    Draft {draft.draft} failed:{" "}
                    <span className="font-mono text-xs">{draft.codes.join(", ")}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}

      {/* The recommendation is the report's headline, then what the agent found, then the cause it settled on. */}
      {(showRecommendation || showConfidence) && (
        <div className="flex flex-wrap items-center gap-2 mb-3">
          {showRecommendation && recommendation && (
            <span
              className={
                "inline-flex items-center rounded-md px-3 py-1 text-base font-semibold ring-1 ring-inset " +
                (ACTION_TONE[recommendation] ?? ACTION_TONE.keep_limit)
              }
            >
              {/* A needs_human report is a fact here, not a proposal: the case was handed to a person. */}
              {recommendation === "needs_human"
                ? "Handed to a person"
                : (RECOMMENDATION_LABELS[recommendation] ?? recommendation)}
            </span>
          )}
          {showConfidence && confidence && (
            <span
              className={
                "inline-flex items-center rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset " +
                (CONFIDENCE_STYLES[confidence] ?? "bg-slate-50 text-slate-600 ring-slate-200")
              }
            >
              {CONFIDENCE_LABELS[confidence] ?? `${confidence} confidence`}
            </span>
          )}
        </div>
      )}

      {report.summary && (
        <p className="text-base text-slate-800 leading-relaxed mb-2">{report.summary}</p>
      )}

      {gaveUp || hideRecommendation ? (
        <p className="text-sm text-slate-600 mb-4">
          Cause not determined
          {openBecause && ` · ${openBecause}`}
        </p>
      ) : (
        likelyCause && (
          <p className="text-sm text-slate-600 mb-4">
            Likely cause: {CAUSE_LABELS[likelyCause] ?? likelyCause}
          </p>
        )
      )}

      <ClaimList
        title="For each review reason"
        items={report.review_reasons?.map(({ reason, finding, evidence_ids }) => ({
          reason,
          text: finding,
          evidence_ids,
        }))}
        onEvidenceClick={onEvidenceClick}
        labelById={labelById}
      />

      <ClaimList
        title="Evidence"
        items={report.evidence?.map(({ claim, evidence_ids }) => ({ text: claim, evidence_ids }))}
        onEvidenceClick={onEvidenceClick}
        labelById={labelById}
      />

      {Array.isArray(report.ruled_out) && report.ruled_out.length > 0 && (
        <div className="mb-4">
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">Ruled out</div>
          <ul className="space-y-2">
            {report.ruled_out.map((item, idx) => (
              <li key={idx} className="text-sm">
                <span className="font-medium text-slate-900">
                  {(item.cause && CAUSE_LABELS[item.cause]) ?? item.cause}
                </span>
                <span className="text-slate-600"> — {item.reason}</span>
                <div className="mt-0.5">
                  <EvidenceIds ids={item.evidence_ids} onClick={onEvidenceClick} labelById={labelById} />
                </div>
              </li>
            ))}
          </ul>
        </div>
      )}

      {report.next_action && (
        <div className="mt-4 border-t pt-3">
          <div className="text-xs uppercase tracking-wide text-slate-500 mb-1">Next action</div>
          <div className="text-slate-700">{report.next_action}</div>
        </div>
      )}

      {(!!report.other_findings?.length || cited.length > 0) && (
        // Open when code gave up: what the specialists found is then all the report holds, and the banner points to it.
        <details className="mt-4" open={gaveUp}>
          <summary className="cursor-pointer text-sm font-medium text-slate-700">Supporting detail</summary>
          <div className="mt-3">
            <ClaimList
              title="Also found"
              items={report.other_findings?.map(({ claim, evidence_ids }) => ({ text: claim, evidence_ids }))}
              onEvidenceClick={onEvidenceClick}
              labelById={labelById}
            />

            {cited.length > 0 && (
              <div>
                <div className="text-xs uppercase tracking-wide text-slate-500 mb-2">Guidelines cited</div>
                <ul className="grid gap-2 sm:grid-cols-2">
                  {cited.map((guideline) => (
                    <li key={guideline.id} className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm">
                      <div className="flex items-baseline gap-2">
                        <span className="font-mono text-xs text-slate-500">{guideline.id}</span>
                        {guideline.title && <span className="font-medium text-slate-900">{guideline.title}</span>}
                      </div>
                      {guideline.when && (
                        <div className="mt-1 text-slate-700">
                          <span className="text-slate-500">When: </span>
                          {guideline.when}
                        </div>
                      )}
                      {!!guideline.allows?.length && (
                        <div className="mt-1 flex flex-wrap items-center gap-1">
                          <span className="text-slate-500">Allows:</span>
                          {guideline.allows.map((allowed) => (
                            <span
                              key={allowed}
                              className="rounded bg-white px-1.5 py-0.5 text-xs font-medium text-slate-700 ring-1 ring-inset ring-slate-200"
                            >
                              {RECOMMENDATION_LABELS[allowed] ?? allowed}
                            </span>
                          ))}
                        </div>
                      )}
                      {guideline.note && <div className="mt-1 text-xs text-slate-500">{guideline.note}</div>}
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}
