import { useAuth0 } from "@auth0/auth0-react";
import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { ErrorBanner } from "../components/ErrorBanner";
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
  // Back from Auth0 while the effect swaps its session for a token. A failed swap leaves PENDING set, so a problem
  // brings the button back rather than leaving the page stuck here.
  const finishing = isAuthenticated && sessionStorage.getItem(PENDING) !== null && !shown;

  return (
    <main className="min-h-screen grid lg:grid-cols-2">
      <section className="bg-slate-900 text-white p-8 lg:p-12 flex flex-col justify-center">
        <h1 className="text-2xl font-semibold">Credit Review</h1>
        <p className="mt-2 max-w-md text-slate-300">
          An AI agent investigates corporate card customers the limit engine holds for review and recommends what to do
          about their limit. A person decides, and the card system applies an approved change.
        </p>
        <ol className="mt-8 max-w-md space-y-3 text-sm text-slate-300 list-decimal list-inside">
          <li>
            The limit engine holds its limit decision for review when it sees a large cut, a failed repayment,
            incomplete bank data or a large limit.
          </li>
          <li>An AI agent investigates and recommends, citing records and policy.</li>
          <li>
            An analyst or a manager decides. A pause or a cut over half the limit needs a manager's authority, so an
            analyst's approval of one goes to a manager.
          </li>
          <li>The card system applies an approved change and returns a receipt.</li>
        </ol>
      </section>

      <div className="flex items-center justify-center bg-slate-50 p-6">
        <div className="w-full max-w-sm rounded-xl bg-white p-8 shadow-sm ring-1 ring-slate-200">
          <h2 className="text-lg font-semibold text-slate-900">Sign in</h2>

          {shown && <ErrorBanner className="mt-4">{shown}</ErrorBanner>}

          <div className="mt-6">
            {finishing ? (
              <p role="status" className="text-sm text-slate-600">
                Finishing sign-in…
              </p>
            ) : (
              <button
                type="button"
                disabled={isLoading}
                onClick={() => {
                  sessionStorage.setItem(PENDING, "1");
                  // "login" asks every time, so the analyst and the manager can take turns on one browser.
                  void loginWithRedirect({ authorizationParams: { prompt: "login" } });
                }}
                className="w-full bg-brand-700 enabled:hover:bg-brand-800 disabled:bg-slate-200 disabled:text-slate-500 text-white font-medium rounded-md px-4 py-2 transition"
              >
                {isLoading ? "Loading…" : "Sign in"}
              </button>
            )}
          </div>
          <p className="mt-3 text-center text-xs text-slate-500">Secured by Auth0</p>
        </div>
      </div>
    </main>
  );
}
