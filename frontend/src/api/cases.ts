import { api } from "./client";
import type { CaseList, CasePath, CaseStatus, CaseView } from "../types";

const BASE = "/api/orchestrator";

export function listCases(params?: { status?: CaseStatus; limit?: number; offset?: number }): Promise<CaseList> {
  const qs = new URLSearchParams();
  if (params?.status) qs.set("status", params.status);
  if (params?.limit) qs.set("limit", String(params.limit));
  if (params?.offset) qs.set("offset", String(params.offset));
  const suffix = qs.toString() ? `?${qs.toString()}` : "";
  return api.get<CaseList>(`${BASE}/cases${suffix}`);
}

export function readCase(caseId: string): Promise<CaseView> {
  return api.get<CaseView>(`${BASE}/cases/${caseId}`);
}

export function readPath(caseId: string): Promise<CasePath> {
  return api.get<CasePath>(`${BASE}/cases/${caseId}/path`);
}

export interface DecisionInput {
  decision: "approve" | "reject";
  note?: string;
  decided?: "keep_limit" | "reduce_limit" | "pause_card" | "request_documents";
}

export function postDecision(caseId: string, input: DecisionInput): Promise<CaseView> {
  return api.post<CaseView>(`${BASE}/cases/${caseId}/decision`, input);
}

/** Dev-only mint of a JWT for the demo. Orchestrator exposes this when DEV_LOGIN=1. */
export function devLogin(sub: string, role: string, customers: string[]): Promise<{ token: string; sub: string; role: string; customers: string[] }> {
  return api.post<{ token: string; sub: string; role: string; customers: string[] }>(
    `${BASE}/dev_login`,
    { sub, role, customers },
  );
}
