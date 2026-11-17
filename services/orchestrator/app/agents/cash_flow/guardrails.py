"""Guardrails that run around the model, not inside it.

Three jobs, all in code, not in the model:

1. memo sanitation — wrap any transaction memo in <memo>...</memo> tags before
   the tool observation reaches the model's context. The system prompt tells
   the model that content inside the tags is data, not instructions. c_009 in
   the gold set is the regression test.

2. evidence attach — collect every evidence id each tool returned in this run;
   any id the model cites that is not in that pool rewrites the finding to a
   could_not_determine entry before the response leaves.

3. cross-line block — the Cash Flow specialist never recommends a limit
   action. Any finding whose claim contains a limit verb is replaced with a
   could_not_determine reason.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from contracts import Evidence, Finding, InvestigateResponse


# Any one of these inside a finding's claim means the model slipped past the
# cross-line rule. We check in lowercase, as whole words.
_LIMIT_VERBS = (
    "keep the limit",
    "keep_limit",
    "reduce the limit",
    "reduce_limit",
    "cut the limit",
    "pause the card",
    "pause_card",
    "request documents",
    "request_documents",
    "needs human",
    "needs_human",
)


# -----------------------------------------------------------------------------
# memo sanitation
# -----------------------------------------------------------------------------

def sanitize_tool_result(result: Any) -> Any:
    """Return a deep copy of the tool result with any `memo` or memo-like text
    field wrapped in <memo>...</memo> tags.

    Applied recursively, so nested dicts and lists (evidence[].data.memo) are
    covered. Non-string memo values are left alone.
    """
    return _sanitize(copy.deepcopy(result))


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _wrap_if_memo(k, _sanitize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _wrap_if_memo(key: str, value: Any) -> Any:
    if isinstance(value, str) and key == "memo" and value:
        # Idempotent: a memo already wrapped stays wrapped.
        if value.startswith("<memo>") and value.endswith("</memo>"):
            return value
        return f"<memo>{value}</memo>"
    return value


# -----------------------------------------------------------------------------
# evidence attach
# -----------------------------------------------------------------------------

class EvidencePool:
    """Every evidence record the agent has seen during this run.

    The agent calls `add(tool_result)` after each tool call; the evidence-attach
    step reads `records` to decide which ids a finding can cite.
    """

    def __init__(self) -> None:
        self.records: dict[str, Evidence] = {}

    def add(self, tool_result: Any) -> None:
        if not isinstance(tool_result, dict):
            return
        for item in tool_result.get("evidence", []):
            if not isinstance(item, dict):
                continue
            eid = item.get("id")
            if not eid or eid in self.records:
                # First writer wins: an aggregate with the same inputs produces
                # the same id, so repeat calls stay consistent.
                continue
            try:
                self.records[eid] = Evidence.model_validate(item)
            except Exception:
                # A malformed evidence item is simply not addable.
                continue

    def known_ids(self) -> set[str]:
        return set(self.records.keys())


def enforce_evidence(findings: list[Finding], pool: EvidencePool) -> tuple[list[Finding], list[str]]:
    """Drop any finding that cites an id not in the pool.

    Returns (kept_findings, could_not_determine_additions).
    """
    kept: list[Finding] = []
    dropped: list[str] = []
    known = pool.known_ids()
    for finding in findings:
        missing = [eid for eid in finding.evidence_ids if eid not in known]
        if missing:
            dropped.append(
                f"A claim was dropped because it cited evidence not in the response: {', '.join(missing)}."
            )
        else:
            kept.append(finding)
    return kept, dropped


# -----------------------------------------------------------------------------
# cross-line block
# -----------------------------------------------------------------------------

def strip_limit_verbs(findings: list[Finding]) -> tuple[list[Finding], list[str]]:
    """Drop any finding whose claim recommends a limit action."""
    kept: list[Finding] = []
    dropped: list[str] = []
    for finding in findings:
        claim_lower = finding.claim.lower()
        if any(_matches(verb, claim_lower) for verb in _LIMIT_VERBS):
            dropped.append(
                "A claim was dropped because it recommended a limit action; the specialist reports facts only."
            )
        else:
            kept.append(finding)
    return kept, dropped


def _matches(verb: str, text: str) -> bool:
    # Underscore forms match verbatim; verb phrases match as whole-word patterns.
    if "_" in verb:
        return verb in text
    pattern = r"\b" + re.escape(verb) + r"\b"
    return re.search(pattern, text) is not None


# -----------------------------------------------------------------------------
# full response assembly
# -----------------------------------------------------------------------------

def build_response(
    *,
    findings: list[Finding],
    pool: EvidencePool,
    could_not_determine: list[str],
    suggested_next: str | None,
) -> InvestigateResponse:
    """Apply both guardrails, keep only the evidence the surviving findings cite.

    Drops are turned into `could_not_determine` entries so the Orchestrator sees
    why a claim did not make it through.
    """
    survived, evidence_drops = enforce_evidence(findings, pool)
    survived, verb_drops = strip_limit_verbs(survived)

    cited = {eid for finding in survived for eid in finding.evidence_ids}
    evidence = [pool.records[eid] for eid in cited if eid in pool.records]

    reasons = list(could_not_determine) + evidence_drops + verb_drops
    # If every finding was dropped and there is no reason, the Pydantic model
    # would refuse the response. Fall back to a generic reason.
    if not survived and not reasons:
        reasons = ["The agent produced no findings and gave no reason."]
    return InvestigateResponse(
        agent="cash_flow",
        findings=survived,
        evidence=evidence,
        could_not_determine=reasons,
        suggested_next=suggested_next,
    )
