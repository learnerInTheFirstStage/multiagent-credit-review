/**
 * Minimal fetch wrapper. Attaches the Authorization header when a token is
 * present in localStorage. All endpoints go through the gateway at /api/*.
 */

// Storage keys for the session. Exported so AuthContext reads/writes the same
// slots client.ts clears on a 401.
export const TOKEN_KEY = "cr_token";
export const USER_KEY = "cr_user";

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

/** Clear everything a 401 or Sign out removes. */
export function clearSession(): void {
  localStorage.removeItem(TOKEN_KEY);
  localStorage.removeItem(USER_KEY);
}

export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

async function request<T>(
  method: "GET" | "POST",
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const token = getToken();
  if (token) {
    headers["Authorization"] = `Bearer ${token}`;
  }
  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let payload: { error?: string; message?: string } = {};
    try {
      payload = await res.json();
    } catch {
      // ignore
    }
    if (res.status === 401) {
      // The token is gone, invalid, or expired. Clear the session and send
      // the user back to /login. Full-page navigation so the React tree
      // re-reads state from localStorage, which is now empty.
      clearSession();
      if (window.location.pathname !== "/login") {
        window.location.assign("/login");
      }
    }
    throw new ApiError(
      res.status,
      payload.error ?? "http_" + res.status,
      payload.message ?? res.statusText,
    );
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body?: unknown) => request<T>("POST", path, body),
};
