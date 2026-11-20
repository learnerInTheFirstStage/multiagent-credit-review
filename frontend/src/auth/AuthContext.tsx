import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { clearSession, getToken, setToken, USER_KEY } from "../api/client";
import type { User } from "../types";

interface AuthContextValue {
  user: User | null;
  token: string | null;
  signIn: (token: string, user: User) => void;
  signOut: () => void;
}

const AuthContext = createContext<AuthContextValue | undefined>(undefined);

function loadUser(): User | null {
  try {
    const raw = localStorage.getItem(USER_KEY);
    return raw ? (JSON.parse(raw) as User) : null;
  } catch {
    return null;
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setLocalToken] = useState<string | null>(() => getToken());
  const [user, setUser] = useState<User | null>(() => loadUser());

  const signIn = useCallback((nextToken: string, nextUser: User) => {
    setToken(nextToken);
    localStorage.setItem(USER_KEY, JSON.stringify(nextUser));
    setLocalToken(nextToken);
    setUser(nextUser);
  }, []);

  const signOut = useCallback(() => {
    clearSession();
    setLocalToken(null);
    setUser(null);
  }, []);

  // Keep context in sync if another tab signs in/out.
  useEffect(() => {
    const handler = () => {
      setLocalToken(getToken());
      setUser(loadUser());
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, []);

  const value = useMemo(() => ({ user, token, signIn, signOut }), [user, token, signIn, signOut]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (ctx === undefined) {
    throw new Error("useAuth must be used inside AuthProvider");
  }
  return ctx;
}
