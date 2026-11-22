import { Link, Navigate, Outlet, Route, Routes, useLocation } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import { useCaseEvents } from "./hooks/useCaseEvents";
import { LiveStateChip } from "./components/LiveStateChip";
import LoginPage from "./pages/LoginPage";
import QueuePage from "./pages/QueuePage";
import CaseDetailPage from "./pages/CaseDetailPage";

function TopBar({ liveState }: { liveState: ReturnType<typeof useCaseEvents> }) {
  const { user, signOut } = useAuth();
  // At 375px the right group gives way: the name truncates (the full one in its title), and the role line and the
  // brand's subtitle show from sm up.
  return (
    <header className="bg-white border-b border-slate-200 px-4 py-3">
      <div className="max-w-5xl mx-auto flex items-center justify-between gap-3">
        <Link to="/" className="shrink-0 text-left">
          <div className="text-sm text-brand-700 font-semibold">Credit Review</div>
          <div className="hidden sm:block text-xs text-slate-500">Limit review queue</div>
        </Link>
        {user && (
          <div className="flex min-w-0 items-center gap-2 text-sm">
            <LiveStateChip state={liveState} />
            <div className="min-w-0 text-right">
              <div className="truncate max-w-[9rem] sm:max-w-none text-slate-900" title={user.name ?? user.sub}>
                {user.name ?? user.sub}
              </div>
              <div className="hidden sm:block text-xs text-slate-500">{user.role}</div>
            </div>
            <button
              onClick={signOut}
              className="whitespace-nowrap text-xs text-slate-500 hover:text-rose-700 border border-slate-200 rounded px-2 py-1"
            >
              Sign out
            </button>
          </div>
        )}
      </div>
    </header>
  );
}

function AuthedLayout() {
  const { token } = useAuth();
  const location = useLocation();
  // Mount the SSE subscription at the authed layout, so it lifecycles with the
  // token: logging in opens the stream, signing out closes it, and the chip in
  // the top bar reflects the current state.
  const liveState = useCaseEvents(token);
  if (!token) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return (
    <div>
      <TopBar liveState={liveState} />
      <Outlet />
    </div>
  );
}

function AppRoutes() {
  const { token } = useAuth();
  return (
    <Routes>
      <Route
        path="/login"
        element={token ? <Navigate to="/" replace /> : <LoginPage />}
      />
      <Route element={<AuthedLayout />}>
        <Route path="/" element={<QueuePage />} />
        <Route path="/cases/:caseId" element={<CaseDetailPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  return (
    <AuthProvider>
      <AppRoutes />
    </AuthProvider>
  );
}
