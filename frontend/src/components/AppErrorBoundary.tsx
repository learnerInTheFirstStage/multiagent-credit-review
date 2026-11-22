import { Component, type ErrorInfo, type ReactNode } from "react";
import { log } from "../lib/log";
import { ErrorBanner } from "./ErrorBanner";

/** A page that fails to render shows what to do and is logged, instead of leaving a blank screen. */
export class AppErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error, _info: ErrorInfo) {
    log.error("the page failed to render", { error: error.name, detail: error.message, path: window.location.pathname });
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="mx-auto max-w-xl p-6">
        <ErrorBanner>
          This page could not be shown. Reload it; if it fails again, the browser console has the details.
          <button className="ml-2 underline" onClick={() => window.location.reload()}>
            Reload
          </button>
        </ErrorBanner>
      </div>
    );
  }
}
