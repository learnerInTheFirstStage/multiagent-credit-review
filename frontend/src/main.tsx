import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router-dom";
import "./index.css";
import App from "./App";
import WithAuth0 from "./auth/WithAuth0";
import { AppErrorBoundary } from "./components/AppErrorBoundary";
import { log } from "./lib/log";

// What no handler caught: an error in an event handler or timer, or a promise nobody awaited.
window.addEventListener("error", (event) => {
  log.error("uncaught error", { error: event.error?.name, detail: event.message, path: window.location.pathname });
});
window.addEventListener("unhandledrejection", (event) => {
  const reason = event.reason instanceof Error ? event.reason : undefined;
  log.error("unhandled promise rejection", { error: reason?.name, detail: reason?.message, path: window.location.pathname });
});

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AppErrorBoundary>
      <WithAuth0>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
          </BrowserRouter>
        </QueryClientProvider>
      </WithAuth0>
    </AppErrorBoundary>
  </StrictMode>,
);
