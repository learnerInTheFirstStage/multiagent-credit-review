import { useAuth0 } from "@auth0/auth0-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "./AuthContext";
import { userFromAccessToken } from "./auth0";

// Set when the person presses the button. Without it, a person who just signed out would be signed straight
// back in from the session Auth0's SDK still holds.
const PENDING = "cr_auth0_pending";

/**
 * The login page when Auth0 is configured. Auth0 sends the person back to /login; their access token
 * then goes into the same session the dev login fills (AuthContext), so the rest of the app is unchanged.
 */
export default function Auth0SignIn() {
  const { isLoading, isAuthenticated, error, user, loginWithRedirect, getAccessTokenSilently } = useAuth0();
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [problem, setProblem] = useState<string | null>(null);

  useEffect(() => {
    if (!isAuthenticated || sessionStorage.getItem(PENDING) === null) return;
    let current = true;
    getAccessTokenSilently()
      .then((token) => {
        if (!current) return;
        sessionStorage.removeItem(PENDING);
        const person = token ? userFromAccessToken(token, user?.email ?? user?.name) : null;
        if (!token || !person) {
          setProblem("This account has no Credit Review role. Ask an admin to give it analyst or manager.");
          return;
        }
        signIn(token, person);
        navigate("/", { replace: true });
      })
      .catch((err: unknown) => current && setProblem(err instanceof Error ? err.message : "Could not get a token."));
    return () => {
      current = false;
    };
  }, [isAuthenticated, getAccessTokenSilently, user, signIn, navigate]);

  // Auth0 reports a refused login (the Action's "needs exactly one role", a wrong password) here.
  const shown = problem ?? error?.message ?? null;

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-100 p-6">
      <div className="w-full max-w-sm bg-white rounded-xl shadow-lg p-8 space-y-6">
        <div className="text-center">
          <h1 className="text-2xl font-semibold text-slate-900">Credit Risk Review</h1>
          <p className="text-sm text-slate-500 mt-1">Sign in with your Credit Review account.</p>
        </div>

        {shown && (
          <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-md px-3 py-2">{shown}</div>
        )}

        <button
          type="button"
          disabled={isLoading}
          onClick={() => {
            sessionStorage.setItem(PENDING, "1");
            // "login" asks every time, so the analyst and the manager can take turns on one browser.
            void loginWithRedirect({ authorizationParams: { prompt: "login" } });
          }}
          className="w-full bg-brand-600 hover:bg-brand-700 disabled:opacity-60 text-white font-medium rounded-md px-4 py-2 transition"
        >
          {isLoading ? "Signing in…" : "Sign in with Auth0"}
        </button>
      </div>
    </div>
  );
}
