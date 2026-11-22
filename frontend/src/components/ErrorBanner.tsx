import type { ReactNode } from "react";

/** Why an action the person took did not go through. role="alert", so a screen reader says it at once. */
export function ErrorBanner({ children, className = "" }: { children: ReactNode; className?: string }) {
  return (
    <div
      role="alert"
      className={"rounded-md border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-800 " + className}
    >
      {children}
    </div>
  );
}
