import { Navigate, Outlet, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { AuthProvider, useAuth } from "./auth/AuthContext";
import LoginPage from "./pages/LoginPage";
import QueuePage from "./pages/QueuePage";
import CaseDetailPage from "./pages/CaseDetailPage";

function TopBar() {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();
  return (
    <header className="bg-white border-b border-slate-200 px-4 py-3">
      <div className="max-w-5xl mx-auto flex items-center justify-between">
        <button onClick={() => navigate("/")} className="text-left">
          <div className="text-sm text-brand-600 font-semibold">Credit Review</div>
          <div className="text-xs text-slate-500">cash-flow signal queue</div>
        </button>
        {user && (
          <div className="flex items-center gap-3 text-sm">
            <div className="text-right">
              <div className="text-slate-900">{user.name ?? user.sub}</div>
              <div className="text-xs text-slate-500">{user.role}</div>
            </div>
            <button
              onClick={signOut}
              className="text-xs text-slate-500 hover:text-rose-700 border border-slate-200 rounded px-2 py-1"
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
  if (!token) {
    return <Navigate to="/login" state={{ from: location }} replace />;
  }
  return (
    <div>
      <TopBar />
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
