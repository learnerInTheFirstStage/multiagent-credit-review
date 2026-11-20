/**
 * Auth0 sign-in settings, read when the bundle is built (VITE_AUTH0_*; see frontend/README.md).
 * With all three set, people sign in through Auth0; with any missing, the dev login stays.
 */
import type { User } from "../types";

const domain = (import.meta.env.VITE_AUTH0_DOMAIN ?? "").replace(/^https:\/\//, "").replace(/\/$/, "");
const clientId = import.meta.env.VITE_AUTH0_CLIENT_ID ?? "";
const audience = import.meta.env.VITE_AUTH0_AUDIENCE ?? "";

export const auth0Settings = domain && clientId && audience ? { domain, clientId, audience } : null;

// Where the post-login Action puts the role and customers (services/auth/auth0/post-login.js).
const CLAIMS = "https://credit-review.demo/";
const ROLES: User["role"][] = ["analyst", "manager", "admin"];

/** The person an Auth0 access token is for, or null when it carries no role this app knows. */
export function userFromAccessToken(token: string, name?: string): User | null {
  const part = token.split(".")[1] ?? "";
  let claims: Record<string, unknown>;
  try {
    const base64 = part.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(part.length / 4) * 4, "=");
    claims = JSON.parse(atob(base64));
  } catch {
    return null;
  }
  const role = claims[`${CLAIMS}role`];
  const customers = claims[`${CLAIMS}customers`];
  if (typeof claims.sub !== "string" || !ROLES.includes(role as User["role"])) return null;
  return {
    sub: claims.sub,
    role: role as User["role"],
    customers: Array.isArray(customers) ? customers.filter((id): id is string => typeof id === "string") : [],
    name,
  };
}
