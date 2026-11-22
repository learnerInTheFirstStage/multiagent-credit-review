import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import type { CaseRow } from "../types";

// Same shape as CaseRow; the SSE handler patches both the list cache and the
// detail cache from this one payload.
type CaseUpdatedEvent = CaseRow;

/**
 * The state the Live chip shows in the top bar.
 */
export type LiveState = "connecting" | "live" | "offline";

const MAX_ERRORS_BEFORE_FALLBACK = 3;

/**
 * Subscribes to GET /cases/events as long as `token` is set.
 *
 * On each `case.updated` event:
 * - patches `qc.getQueryData(["case", id])` so an open case page updates in place
 *   when only row fields changed; a new status or step waits for the re-fetch;
 * - invalidates `["cases"]` so the queue re-fetches (merging open / closed sets
 *   is non-trivial, so letting Query re-run is simpler than patching).
 *
 * Fallback: on a hard failure (server 401/5xx → EventSource CLOSED) the stream
 * stops immediately; on transient drops the browser auto-reconnects, and after
 * three consecutive errors we give up. In both cases the LiveStateChip reads
 * "Polling" and the existing Queue / CaseDetail polling keeps the UI fresh.
 *
 * Returns the `LiveState` so the caller can show the chip.
 */
export function useCaseEvents(token: string | null): LiveState {
  const qc = useQueryClient();
  const [state, setState] = useState<LiveState>("connecting");
  // We need a ref here so the error counter survives EventSource auto-reconnects
  // without resetting on every render.
  const errorsRef = useRef(0);

  useEffect(() => {
    if (!token) {
      setState("offline");
      return;
    }
    errorsRef.current = 0;
    setState("connecting");

    const url = `/api/orchestrator/cases/events?token=${encodeURIComponent(token)}`;
    const source = new EventSource(url);

    source.addEventListener("open", () => {
      errorsRef.current = 0;
      setState("live");
      // Every new connection takes a silent baseline on the server. Any case
      // that changed between the drop and the reconnect gets absorbed into the
      // baseline and never emitted, so the queue could sit stale with a green
      // Live chip. One invalidate at open narrows the window. It does not
      // fully close it: Starlette sends `http.response.start` (which fires
      // this `open`) before the server's first _scan runs, so there's still
      // a tens-of-ms window where this GET reads S0 and the baseline later
      // reads S1, swallowing the change. Polling heals it within 5 s.
      qc.invalidateQueries({ queryKey: ["cases"] });
    });

    source.addEventListener("case.updated", (event: MessageEvent) => {
      try {
        const payload = JSON.parse(event.data) as CaseUpdatedEvent;
        // Patch an open case page's row-level fields so status / outage reflect
        // the change within one frame. The updater returns `previous` unchanged
        // (a no-op) when nothing is cached: SSE sends row-shaped data, so
        // writing it as the detail cache would create a partial CaseView (no
        // report, decisions, path) that CaseDetailPage would render as real.
        // A new status or step is not patched in: with the old decisions, report
        // and Card answer it shows a state the case was never in (a closed case
        // with no action, read as "recorded but not carried out"). The re-fetch
        // below brings the whole new state at once.
        const hadDetail = qc.getQueryData(["case", payload.case_id]) !== undefined;
        qc.setQueryData<CaseRow & { [k: string]: unknown }>(["case", payload.case_id], (previous) =>
          previous && previous.status === payload.status && previous.waiting_for === payload.waiting_for
            ? { ...previous, ...payload }
            : previous,
        );
        // If the detail page is actually open, re-fetch the full CaseView so
        // report / decisions / path are fresh too, not just the row fields.
        if (hadDetail) {
          qc.invalidateQueries({ queryKey: ["case", payload.case_id] });
          qc.invalidateQueries({ queryKey: ["case-path", payload.case_id] });
        }
        // The queue keeps several caches (open, closed, the reads of who
        // approved last); patching every one would be messy. One invalidate
        // re-runs any active ["cases", ...] query. Note: `invalidateQueries`
        // with its v5 defaults cancels in-flight GETs and starts a new one, so
        // a burst of events would cause a burst of GETs. The backend's silent
        // baseline (see events.py _stream) keeps the burst empty on first connect.
        qc.invalidateQueries({ queryKey: ["cases"] });
      } catch {
        // Bad JSON on a single event is not worth going offline over.
      }
    });

    source.addEventListener("error", () => {
      // EventSource auto-reconnects when the transport drops (readyState is
      // CONNECTING at this point). When the server answers with a non-2xx
      // status it goes to CLOSED permanently — 401 on a bad token, 502 / 503
      // while the orchestrator is down — and the browser does not retry.
      if (source.readyState === EventSource.CLOSED) {
        // Hard failure: no reconnect. Hand over to polling immediately. The
        // fetch wrapper handles 401 — so if the token is truly bad, the next
        // ["cases"] poll fires clearSession() and the /login redirect.
        setState("offline");
        return;
      }
      // Transient drop. Reflect the reconnect state in the chip, and give up
      // after three strikes so a flaky connection doesn't look "Live" forever.
      setState("connecting");
      errorsRef.current += 1;
      if (errorsRef.current >= MAX_ERRORS_BEFORE_FALLBACK) {
        source.close();
        setState("offline");
      }
    });

    return () => {
      source.close();
    };
  }, [token, qc]);

  return state;
}
