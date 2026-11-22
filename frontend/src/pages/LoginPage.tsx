import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { ApiError } from "../api/client";
import { devLogin } from "../api/cases";
import { useAuth } from "../auth/AuthContext";
import Auth0SignIn from "../auth/Auth0SignIn";
import { auth0Settings } from "../auth/auth0";
import { ErrorBanner } from "../components/ErrorBanner";

const DEFAULT_CUSTOMERS = [
  "c_001",
  "c_002",
  "c_003",
  "c_004",
  "c_005",
  "c_006",
  "c_007",
  "c_008",
  "c_009",
  "c_010",
  "c_011",
  "c_012",
  "c_013",
];

type RoleChoice = "analyst" | "manager";

// Auth0 when the build has its settings (src/auth/auth0.ts); the dev login otherwise.
export default function LoginPage() {
  return auth0Settings ? <Auth0SignIn /> : <DevLoginPage />;
}

function DevLoginPage() {
  const { signIn } = useAuth();
  const navigate = useNavigate();
  const [sub, setSub] = useState("u_analyst");
  const [role, setRole] = useState<RoleChoice>("analyst");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const resp = await devLogin(sub.trim(), role, DEFAULT_CUSTOMERS);
      signIn(resp.token, { sub: resp.sub, role: resp.role as RoleChoice, customers: resp.customers });
      navigate("/", { replace: true });
    } catch (err) {
      const message =
        err instanceof ApiError
          ? err.status === 404
            ? "Dev login is disabled. Set DEV_LOGIN=1 on the orchestrator."
            : err.message
          : "Something went wrong.";
      setError(message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-slate-100 p-6">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm bg-white rounded-xl shadow-lg p-8 space-y-6"
      >
        <div className="text-center">
          <h1 className="text-2xl font-semibold text-slate-900">Credit Review</h1>
          <p className="text-sm text-slate-500 mt-1">Demo sign-in.</p>
        </div>

        <div className="space-y-4">
          <label className="block">
            <span className="text-sm font-medium text-slate-700">Demo user</span>
            <input
              type="text"
              value={sub}
              onChange={(event) => setSub(event.target.value)}
              className="mt-1 w-full border border-slate-300 rounded-md px-3 py-2 focus:outline-none focus:ring-2 focus:ring-brand-500"
              placeholder="u_analyst"
              required
            />
          </label>

          <fieldset>
            <legend className="text-sm font-medium text-slate-700 mb-2">Sign in as</legend>
            <div className="grid grid-cols-2 gap-2">
              {(["analyst", "manager"] as RoleChoice[]).map((choice) => (
                <button
                  key={choice}
                  type="button"
                  onClick={() => setRole(choice)}
                  className={
                    "border rounded-md px-3 py-2 text-sm font-medium transition " +
                    (role === choice
                      ? "border-brand-600 bg-brand-50 text-brand-700"
                      : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50")
                  }
                >
                  {choice.charAt(0).toUpperCase() + choice.slice(1)}
                </button>
              ))}
            </div>
          </fieldset>
        </div>

        {error && <ErrorBanner>{error}</ErrorBanner>}

        <button
          type="submit"
          disabled={loading}
          className="w-full bg-brand-600 hover:bg-brand-700 disabled:opacity-60 text-white font-medium rounded-md px-4 py-2 transition"
        >
          {loading ? "Signing in…" : "Sign in"}
        </button>

        <p className="text-xs text-slate-500 text-center">
          Your token covers {DEFAULT_CUSTOMERS.length} demo customers.
        </p>
      </form>
    </div>
  );
}
