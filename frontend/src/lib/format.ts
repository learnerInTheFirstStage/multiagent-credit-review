/**
 * Formatting helpers used across the review page.
 */

export function dollarsFromCents(
  cents: number | null | undefined,
  options: { precision?: number } = {},
): string {
  if (cents === null || cents === undefined) return "—";
  const dollars = cents / 100;
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: options.precision ?? 0,
  }).format(dollars);
}

/** Basis points (10 000 = 100%) to a human percent. */
export function percentFromBp(bp: number | null | undefined, decimals = 2): string {
  if (bp === null || bp === undefined) return "—";
  return `${(bp / 100).toFixed(decimals)}%`;
}

export function percentDelta(from: number, to: number): string {
  if (from === 0) return "—";
  const raw = ((to - from) / from) * 100;
  const pct = Math.round(raw);
  // A tiny cut that rounds to zero should still read as negative, not +0%.
  if (pct === 0 && raw !== 0) return raw < 0 ? "<-1%" : "<+1%";
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct}%`;
}
